# Updating a live AgentStack Mail service

The cutover procedure (`agentstack-mail-cutover.md`) covers replacing the
third-party server once. This document covers every deployment after that:
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

6. **Prove production, then say done.** `launchctl print` state is not the
   goal; repeat the step-3 probes against the real port, and confirm the
   database file being served is the production one (`health_check` reports
   `database_url`). Record probes, `$SHA`, and both render paths in a
   deployment note.

## Rollback

`start` the previous render's ownership manifest again (its candidate venv
was never touched). Rollback is a repeat of step 5 with the arguments
swapped — which is why neither old render nor old candidate is ever deleted
by an update.

## Relation to cutover receipts

The cutover receipts pin the candidate that performed the 2026-08 cutover.
Deploying a newer candidate does not rewrite that history — receipts describe
events, not the currently-active build. What *would* orphan them is editing
the pinned candidate in place, which this procedure forbids.
