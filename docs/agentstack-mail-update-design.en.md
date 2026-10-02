# Replacing the ORRERY Mail build: design note

> 日本語版: [agentstack-mail-update-design.md](agentstack-mail-update-design.md)
> The procedure and behaviour are specified in [agentstack-mail-update.en.md](agentstack-mail-update.en.md). This note keeps why it has this shape, and what is still undecided (whether to make it the default).

## Starting point (as of 2026-09-30)

- A re-run installer that finds ORRERY Mail answering the endpoint checks the shared database with `health_check` and **adopts** it, leaving the build alone (`resolve_native_mail_connection` → `adopt_running_native_mail_render`). The build only changed on the provisioning path, when nothing answered.
- Mail changes (for example `register_agent(existing_agent_id=…)` in 2026.09.30.4) therefore never reached users through an ordinary update. The only route was the manual runbook: hold the unit, stop, run the installer.
- Real damage: older-format Claude children hit `mail_schema_unsupported` and fall back to a conversation-only resume (HumbleLavoisier).
- The pieces for a switch already existed. Candidates (`candidates/<commit>/venv`) and renders (`renders/<commit>-<hash>/`) are immutable and the previous ones stay on disk. `agentstack-mailctl stop` stops only the runner it started, holds the autostart sweep back with its stop marker, and `start` clears it. `server_instance_id` lives in the database's `mail_instances`. The server uses stateless HTTP.

## Design

`--update-mail` switches to this checkout's candidate after the adoption path has identified the running deployment.

1. The stages that can fail (building the candidate, verifying it on a scratch port, backing up the database) run while the running Mail keeps serving. A failure there stops nothing (`not-switched`).
2. Stop to start is the outage. Health for the new build must name the shared database, and the pidfile must name the new runner.
3. If that does not hold, the previous render is started as it was (`rolled-back`). The previous candidate, render and runner are immutable, so what comes back is exactly what was running.
4. `env.sh`, the autostart unit, `connections/local.json` and `install-state.json` are written **after** the switch, from whichever deployment is actually serving. After a rollback the previous deployment is recorded. That is what "restore the previous manifest" amounts to: nothing rewinds a manifest, because it is written only once the outcome is settled.
5. An interrupt (Ctrl-C, SIGTERM, HUP) is settled like a failure before exiting. During verification, the scratch server is stopped and the snapshot deleted (the snapshot copies every message and token, so leaving it would expose them). Between the stop and writing `env.sh`, the new build is left alone if `env.sh` already names it; otherwise the previous build is put back. Long waits run in the background under `wait`, so a signal is handled at once.

### What is not lost

| Item | Handling |
| --- | --- |
| Live connections | Stateless HTTP, so there are no sessions. Requests during the outage are refused and recover on retry |
| Tokens, credentials | In the database and in client-side files; neither is touched |
| Enrollment pin | `server_instance_id` is in the database and unchanged. `expected_server_instance_id` is preserved; only `mail_env` is updated |
| Database | Same state root. Startup DDL must be additive (verification checks for removed or redefined tables, columns, indexes and triggers, and `quick_check`). Backed up just before the switch |
| Archive, signals, management socket | Same paths. The verification server points entirely at scratch (its env comes from the same function as the production render) |
| Autostart | The stop marker holds the sweep back during the switch; `start` clears it |

The database is never rolled back automatically: that would discard messages written after the switch. The backup is for a person to use by decision.

## Options: should it be the default?

| Option | Behaviour | For | Against |
| --- | --- | --- | --- |
| A. Explicit opt-in (implemented) | Switch only with `--update-mail`; without it, a `notice:` shows the difference | A person chooses when to take the outage. The meaning of a re-run does not change | Forget the flag and Mail changes still do not arrive (though the notice makes that visible) |
| B. Switch by default | Switch whenever the build differs; `--keep-mail` suppresses it | Arrives with no action | Every re-run carries an outage, including at busy times |
| C. Default only when needed | Switch by default only when the new build declares it is needed (for example, children require its Mail schema) | Needed changes arrive, no needless outage | Needs a way to declare and detect "needed", which does not exist yet |

**Recommendation: A for now.** Run `--update-mail` a few times on a live machine, and move towards C once outage and rollback measurements are in. B is not recommended: it imposes an outage on every agent at every re-run. Between commits whose package tree is identical, A neither switches nor prints the notice.

The final decision is the maintainer's.


## Decision (2026-10-02): three modes, the default stays keep

There are now three modes: `--keep-mail` (the default), `--update-mail` (strict) and `AGENTSTACK_MAIL_UPDATE=auto` (opt-in). The outcome is machine-readable through the final `mail-result:` line and the `mail-plan:` line of `install.sh --print-mail-plan` ([agentstack-mail-update.en.md](agentstack-mail-update.en.md#replacing-the-build-with---update-mail)); setup.sh and update.sh read them.

`auto` switches only for a deployment managed by `agentstack-mailctl`, after verification passes, and when the expected outage (the time the candidate took to answer against the snapshot during verification, plus 1.5 s) is within `AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET` (default 8 s); otherwise it keeps the running build and exits 0. Outage measurements (2026-10-02, temporary HOME, separate port): a stop takes under a second and a start on an empty database 1 to 2 s. The stdio proxy (children) and top-level Codex come back on their next call (Codex even after 71 s); top-level Claude Code (direct HTTP) reconnected by itself after outages up to 15 s and needed a reconnect from `/mcp` after 18 s or more.

**What must be closed before `auto` becomes the default** (design review, 2026-10-02):

1. **A ceiling on the outage.** The estimate is for a successful start and does not prevent a long outage on failure. Set a deadline from the stop, and fit giving up on the new build and starting the previous one inside it (including the rollback and health waits).
2. **Being able to go back.** Check on the snapshot that the previous build can read and write the database after the new build opened it (existing owner tokens, messages, reservations). An update that cannot be checked goes to an explicit `--update-mail`. Unchanged DDL does not prove unchanged data.
3. **Older update.sh.** The published cockpit update.sh runs install.sh with no option, so its users would be switched the moment `auto` became the default. Change the default only once update.sh and setup.sh always pass an explicit mode, and say in the CHANGELOG that an older update.sh then gets `auto`.
