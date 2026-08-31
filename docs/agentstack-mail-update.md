# Updating a live AgentStack Mail service

The one-time authority handoff from the third-party server is history and its
runbook is not published. This document covers every deployment after it:
shipping a new build of `agentstack_mail` onto a machine where the service is
already serving production traffic.

## Principles

- **Candidates are immutable.** Never install into, upgrade, or edit the venv
  the running job uses. Build a new venv named by the exact commit
  (`final-candidate-<sha>/venv`), verify it, and switch launchd to it. The
  previous candidate stays on disk untouched — it *is* the rollback.
- **One render per deployment.** The plist, env file, and ownership manifest
  for a deployment live together in one render directory and are never edited
  after `start`. A new deployment gets a new render directory.
- **The database is shared state, not part of the candidate.** `--state-root`
  stays fixed across deployments. A build whose `ensure_schema` would alter
  the schema needs its own migration plan and is out of scope here.
- **Verify before the switch, not after.** The only step allowed to affect
  production is the stop/start pair; everything before it must run against a
  scratch port and scratch database.

## Procedure

Definitions: `MAINT=~/.agentstack/cutover-maintenance`, `SHA` = the exact
commit being deployed, `NEW=$MAINT/final-candidate-$SHA`.

1. **Build the candidate from the exact commit.**

   ```bash
   git -C <repo> rev-parse HEAD          # must equal $SHA; a dirty tree disqualifies
   python3 -m venv "$NEW/venv"
   "$NEW/venv/bin/pip" install <repo>/packages/agentstack_mail
   ```

2. **Gate on the test suite at that commit** (dev venv, see
   `CONTRIBUTING.md`): `PYTHONPATH=. .venv/bin/python -m pytest -q` must be
   green in a clean single run.

3. **Verify the candidate offline.** Copy the live env file, change only the
   port, point the database at a scratch copy, and run the candidate in the
   foreground. Probe what production will need — at minimum a
   `health_check` `tools/call` on the canonical path **and on each alias**
   (`/api/`, `/mcp`), plus one representative read (`whois`).

4. **Render the new deployment** (new directory, live env values):

   ```bash
   "$NEW/venv/bin/agentstack-mail-service" render \
     --output-dir "$MAINT/deploy-$SHA-$(date +%Y%m%d%H%M)/render" \
     --service-executable "$NEW/venv/bin/agentstack-mail-service" \
     --server-executable  "$NEW/venv/bin/agentstack-mail" \
     --env-file  <live env file copied into the new render dir> \
     --state-root ~/.agentstack/mail \
     --label org.orrery.mail
   ```

   Keep the `AGENTSTACK_MAIL_LEGACY_LAUNCHD_*` keys: the same-port production
   guard in `service_start` still requires them.

5. **Switch.** This is the outage window (seconds, not minutes):

   ```bash
   "$NEW/venv/bin/agentstack-mail-service" stop  --ownership-manifest <OLD render>/org.orrery.mail.ownership.json
   "$NEW/venv/bin/agentstack-mail-service" start --ownership-manifest <NEW render>/org.orrery.mail.ownership.json
   ```

6. **Point whatever restarts mail at the new deployment.** A machine that keeps
   the service up across logins usually has a supervisor — here, a launchd job
   every five minutes that starts mail when the endpoint does not answer. If it
   names a candidate and a manifest of its own, `stop`/`start` has not finished
   the deployment: the next time mail goes quiet for any reason, that supervisor
   brings the *old* build back, and the rollback is silent because the endpoint
   answers again afterwards.

   This is not hypothetical. The 2026-08-25 deployment of `72b76aa` was undone
   at 2026-08-26 14:37 by exactly this path, and nobody noticed until an
   unrelated bug report two days later showed the old code running.

   Keep the supervisor's target in one place that the deployment updates —
   a pointer file it reads, not a path baked into the script:

   ```bash
   cat > ~/.agentstack/mail/runtime/current-deployment.env <<EOF
   ORRERY_MAIL_SERVICE=$NEW/venv/bin/agentstack-mail-service
   ORRERY_MAIL_MANIFEST=$MAINT/deploy-.../render/org.orrery.mail.ownership.json
   EOF
   ```

   Then confirm the supervisor resolves the new paths before you walk away.

7. **Prove production, then say done.** `launchctl print` state is not the
   goal; repeat the step-3 probes against the real port, and confirm the
   database file being served is the production one (`health_check` reports
   `database_url`). Record probes, `$SHA`, and both render paths in a
   deployment note.

## Rollback

`start` the previous render's ownership manifest again (its candidate venv
was never touched). Rollback is a repeat of step 5 with the arguments
swapped — which is why neither old render nor old candidate is ever deleted
by an update. Point the supervisor back as well (step 6), or the next restart
undoes the rollback.

## Checking which build is actually serving

The deployment you performed and the build answering the port are different
claims. Ask the port, not your notes:

```bash
pid=$(lsof -nP -iTCP:<port> -sTCP:LISTEN | awk 'NR>1{print $2}')
ps -o command= -p "$pid"          # which candidate's python is this
```

Note that the pid launchd reports is the wrapper, not the server; the server
is its child, and it is the child that holds the descriptors and answers
requests. Measuring the wrapper and concluding "the fix is live" is a mistake
that has already been made here.

## Relation to cutover receipts

The cutover receipts pin the candidate that performed the 2026-08 cutover.
Deploying a newer candidate does not rewrite that history — receipts describe
events, not the currently-active build. What *would* orphan them is editing
the pinned candidate in place, which this procedure forbids.
