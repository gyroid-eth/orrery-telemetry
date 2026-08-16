# AgentStack mail extraction

`agentstack-mail` is the public installer's default coordination provider. It
is developed inside this repository as a logically isolated package;
repository extraction remains deferred until the versioned contract and
independent export/test gates are stable.

The authoritative implementation input is the working live Python AgentMail
checkout, including its local commits and dirty signal/runtime fixes. Current
upstream is retained only as an advisory security and bug-fix source.

The default endpoint is `http://127.0.0.1:18765/mcp`; data lives below
`~/.agentstack/mail`. Set `AGENTSTACK_MAIL_PROVIDER=upstream` when running
`scripts/install.sh` to opt out and use the third-party provider. No test or
installer may point both services at one writable database or archive.

The caller-derived compatibility surface is versioned in
`packages/agentstack_mail/fixtures/compatibility-tools-v1.json`. Its 24 tools
are the positive union of executable callers and shipped model-facing
contracts. Permission deny entries, negative instructions, and Codex
Bridge-local operations do not become source-extraction roots.

The implementation train was:

1. freeze provenance, live tool schemas, and the caller-derived tool contract;
2. define isolated configuration and an exact-schema database copy/import gate;
3. port identity, messaging/contact, receipt, reservation, and notification
   behavior with differential tests against the live source;
4. port HTTP and lifecycle stability without the machine-specific notify and
   tmux daemons;
5. update installer, doctor, bridge, and hooks atomically to the new endpoint
   and authentication while preserving each client's existing MCP key;
6. run coexistence, migration, rollback, fault, and real-machine soak evidence
   before the approved authority switch.

The first four gates are executable and hermetic via
[`packages/agentstack_mail/scripts/cutover_gates.py`](../packages/agentstack_mail/scripts/cutover_gates.py).
The automated contract and the still-manual real-machine soak procedure are in
[`agentstack-mail-cutover-gates.md`](agentstack-mail-cutover-gates.md). Passing
them is evidence rather than authority on its own; the owner approved the
public authority switch on 2026-08-15.

The provider identity remains `agentstack-mail`, but it is not the client
registration key. First cutover preserves Claude's `mcp-agent-mail` and
Codex's `agent-mail` keys so fully qualified tool names, permissions, and hook
matchers do not change. The authority is determined by endpoint, data roots,
and ownership, never by the client-visible key. Optional key renaming and stale
selector cleanup are separate post-cutover work.

## Current core boundary

The core train copies the live data/archive/tool-body seam into the renamed
package and publishes exactly the 24 versioned tools through a fail-closed
FastMCP subclass. MCP resources and the 16 non-compatibility tools are not
published. Their bodies remain internal only until the differential train can
prove that pruning them does not break macro or storage dependencies.

Because no roster resource is published, tool descriptions direct callers to
the identity assigned by the AgentStack runtime or returned by
`register_agent`/`macro_start_session`. `list_contacts` returns known links,
`whois` verifies a known identity, and broadcast delivery does not require a
roster response. Tool filtering cannot reduce the public surface: a profile
that removes any contract tool makes server construction fail closed.

All production settings use the `AGENTSTACK_MAIL_*` namespace. With no new
settings present, the resolved port is `18765` and database, archive, and
signals are below `~/.agentstack/mail`; legacy unprefixed variables and a CWD
`.env` are ignored. An installed package now exposes `agentstack-mail`, which
serves the exact boundary on the loopback-only default
`http://127.0.0.1:18765/mcp`. The first entry point rejects non-loopback binds
and bearer/JWT settings rather than pretending to enforce authentication.
`agentstack-mail --help` exits without starting a server; `--host`, `--port`,
and `--path` override the namespaced endpoint settings for that process.
Identity mode behavior remains frozen-source compatible: default `coerce` may
return a generated name for a noncanonical explicit request, and an invalid
mode falls back to `coerce`. The cutover profile must therefore set
`AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough` for the fixed runtime
identities; the legacy unprefixed key remains intentionally isolated. The
Claude Code registration hook compares every explicit request with the returned
name before recording success; a mismatch or unreadable response exits nonzero.
This is a caller-side refusal, not a transaction rollback: because it
runs after the tool call, a substituted server row may remain, and sessions with
an existing `AGENT_NAME` are not universally stopped by the success-flag guard.
Codex has no equivalent PostToolUse hook: reserved bootstrap and
reregister paths already stop on mismatch, while direct spawn, raw MCP calls,
and Codex App reauthentication remain follow-up coverage rather than a substitute
for the required cutover setting.
The service helper/controller and copy/verify/rollback-assess migration
commands are implemented. The installer provisions this provider by default
while preserving Claude's `mcp-agent-mail` and Codex's `agent-mail` client
keys.

## Installation and lifecycle

The regular installer provisions the bundled package into an immutable
candidate virtual environment, renders a namespaced service environment, and
starts a supervised-background runner. The runner restarts a crashed server
after five seconds; `agentstack-mail-service foreground` holds the state-root
authority lock so a restart cannot create two writers. The thin lifecycle
controller adds a PID file, exact rendered-runner identity check, endpoint and
database health check, and a short-lived operation lock:

```bash
~/.agentstack/bin/agentstack-mailctl start
~/.agentstack/bin/agentstack-mailctl status
~/.agentstack/bin/agentstack-mailctl stop
~/.agentstack/bin/agentstack-mailctl restart
```

The controller deliberately does not add a second supervision layer of its own.
A live PID with the wrong command, a healthy endpoint without the owned PID
file, or an endpoint reporting another database is refused rather than stopped
or reused.

### Restart after a reboot

The runner is started with `nohup`, which does not survive a reboot, so the
installer registers a *supervising trigger* — `org.agentstack.mail` as a launchd
job on macOS, or a oneshot service plus a `.timer` on Linux — whose only job is
to run `agentstack-mailctl start` at login and every five minutes thereafter. It is registered on every install,
including when the installer finds a healthy server already running, so
re-running `install.sh` on an existing setup is enough to gain it.

The unit carries only `HOME`, `AGENTSTACK_HOME` and `PATH`: `agentstack-mailctl`
reads `env.sh` for everything else, so the trigger runs exactly the command an
operator runs by hand and picks up a re-rendered service env automatically.
Freezing those paths into the unit instead would silently keep starting the
previous render after a re-install. Each invocation is one-shot on purpose (`KeepAlive` false / `Type=oneshot`): the
controller hands the server to `nohup` and exits, so a restart-always unit would
respawn the *controller* in a loop instead of supervising the server. Repetition
comes from `StartInterval` on launchd and from the timer on systemd. `start` is
idempotent — it reports "already running" and exits 0 when the owned PID is alive
and healthy — so re-running it costs nothing, and it stays silent when there is
nothing to do. Its output goes to `agentstack-mail-autostart.log` (launchd
`StandardOutPath`, systemd `StandardOutput=append:`), separate from the server's
own log.

`agentstack-mailctl stop` is honoured. It records the intent in
`runtime/agentstack-mail.stopped`, and the sweep leaves a deliberately stopped
server alone until an explicit `start` or `restart` releases the hold. Without
that record the trigger would quietly undo an operator's stop at the next
firing — measured before the fix: `stop` reported "AgentStack Mail stopped", and
the following sweep reported "AgentStack Mail started".

If neither launchd nor systemd is available, the installer says so explicitly
rather than skipping quietly, because a missing autostart is invisible until the
machine actually reboots. That is not hypothetical: on 2026-08-16 a reboot on the
maintainer's Mac came back with the dashboard running, no mail server, and a
stale legacy service holding port 8765 — every agent registered afterwards wrote
to the wrong database, and nothing reported an error.

`agentstack-uninstall` removes the trigger — both the launchd/systemd job and the
unit file — along with the other services recorded in the install manifest.

**What it covers.** The rendered runner restarts a crashed *server* after five
seconds. If the *runner itself* is killed the trigger picks it up on its next
sweep (measured before the sweep existed: pidfile present, port closed, nothing
restarting it until the next login). A stale PID with a free port recovers
automatically; a stale PID whose port is held by an unhealthy or foreign listener
is refused with a message rather than fought over — the sweep will retry, but it
will not evict a listener it does not own. Immediate recovery is
`agentstack-mailctl start`.

The upstream opt-out provider (`AGENTSTACK_MAIL_PROVIDER=upstream`) has the same
reboot behaviour and no equivalent controller to trigger, so the installer warns
about it instead of registering anything.

## Manual migration from upstream

Migration is an operator-run procedure, not an installer step. First quiesce
the upstream writer and determine the canonical absolute database, archive,
and signals paths. The destination must not yet exist. From the repository
checkout, copy and then verify all three projections:

```bash
LEGACY_DB=/absolute/path/to/storage.sqlite3
LEGACY_ARCHIVE=/absolute/path/to/git_mailbox_repo
LEGACY_SIGNALS=/absolute/path/to/signals
DESTINATION="$HOME/.agentstack/mail"

uv run --project packages/agentstack_mail agentstack-mail-migrate copy \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

uv run --project packages/agentstack_mail agentstack-mail-migrate verify \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

./scripts/install.sh
```

This path was used for the 2026-08-12 live switch: the database plus archive,
about 60,000 records in total, were copied and reconciled successfully. Keep
the upstream service stopped between copy and verification so the source
snapshot does not change under the verifier.

## Rollback

Run `AGENTSTACK_MAIL_PROVIDER=upstream ./scripts/install.sh`; the AgentStack Mail data remains in `~/.agentstack/mail`.

## Notification layout compatibility

AgentStack Mail writes one signal per message at
`signals/projects/<project>/agents/<agent>/<message-id>.signal`. The bundled
`hooks/watch_agent_mail_signals.sh` recursively discovers that layout, extracts
the nested `message` metadata, injects the notification, and removes only the
successfully delivered per-message signal. The repository installer regression
test exercises that exact producer-shaped path in an isolated signals/runtime
root with a fake tmux boundary; it never touches the live watcher or ports.

File-reservation activity probes converge on upstream #240's one-pathspec Git
walk, then add a process-global concurrency limit of eight, a three-second
per-probe deadline, and a four-second status-pass budget. A timed-out, failed,
or incomplete filesystem/Git probe is explicit unknown activity and therefore
cannot trigger stale auto-release; TTL expiry is unchanged. The package-local
performance gate repeats 57 concrete tracked paths five times, requires a
six-second-or-better median and at least three fully matched/complete runs, and
reports the maximum separately. Fingerprints exclude mutable activity
timestamps.

## Archive commit latency and startup repair

Archive-writing tools durably update SQLite and write their audit files before
returning. The Git commit for those files is queued asynchronously by default,
so Git history construction is not part of request latency. Set
`AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC=false` to restore the old synchronous
behavior; this kill switch remains available if a deployment observes queue or
commit failures.

The trade-off is limited to the Git projection. A hard process or machine
shutdown can cancel a commit after the tool has returned. The database remains
committed and the audit files remain as uncommitted files in the archive working
tree. At the next server startup, the existing archive heal pass removes stale
lock artifacts, discovers untracked and modified audit files, and commits them
synchronously before the service starts accepting work. A recovery or
maintenance failure is logged but does not discard the database or files.

Startup also checks `git gc --auto`, rate-limited by a marker under `.git` to at
most once every 24 hours. The daily limit avoids paying even the object-count
check on every restart; `--auto` supplies the second gate, so a repack runs only
when Git's own loose-object or pack thresholds say it is warranted. When Git
does start maintenance, `gc.autoDetach=false` keeps completion and failure
observable to the startup heal pass.

For scale, the 2026-08-14 Tier-1 measurements were taken on the development
MacBook Pro, with Python 3.12.2, an ephemeral loopback server, an empty scratch
archive, 25 measured iterations after three warmups, and the production-shaped
tool log enabled. With `commit_async=true`, register/send/
reservation p50 values were 32/76/47 ms (p95 41/89/58 ms); on the same machine
and scratch-archive shape with `commit_async=false`, they were 217/310/259 ms
(p95 252/344/271 ms). These are comparison data for that machine and profile,
not universal latency promises. The executable gate and full recorded settings
live in [`bench/tier1_latency.py`](../bench/tier1_latency.py) and
[`bench/README.md`](../bench/README.md).

The provenance Git bundle and dirty patch remain repository-only audit inputs
and are excluded from wheels and source distributions. Distribution gates
verify both artifact types still contain the runtime modules, NOTICE, both
licenses, and the versioned fixtures.

## Behavior differential gate

The approved Core base full SHA is owned only by
`fixtures/differential-expected-divergences-v2.json`; prose does not mirror
it. Artifact verification byte-matches the packaged fixture to that checkout
fixture and accepts the base only when its commit object is reachable from a
persistent local branch, remote-tracking branch, or tag. CI fetches full
history so a shallow/unfetched object and an existing-but-unreachable object
produce distinct failures. The approved base is a review anchor, not the
candidate: local and push lanes use the exact checked-out `HEAD`, while a
pull-request lane uses the exact synthetic merge `HEAD` checked out by that
lane. That same full candidate SHA must be supplied to exact-checkout,
`candidate-source-bound`, and every candidate-bound evidence verifier; the
two SHAs are never substituted for one another. Behavior tests authenticate
and reconstruct the frozen live baseline from the checked-in Git bundle and
dirty patch, then start live and Core in separate subprocesses. Worker
environments inherit only
an OS bootstrap allowlist; database, archive, signals, home, temporary files,
Git identity, port, and import roots are explicitly isolated. Test inputs and
outputs are private, symlink escape and source-origin drift fail closed, and no
developer AgentMail checkout is consulted.

The ordered scenarios are:

1. identity, contact, messaging, topic/inbox, mark-read, acknowledgement replay,
   reply, full-text search, and heuristic thread summary;
2. Unicode reservation idempotency/conflict/renew/release plus per-message
   signals and BCC privacy;
3. health, start-session, reservation-cycle, contact-handshake, summary fetch,
   and retirement lifecycle.

Their union is exactly the versioned 24 tools. Each operation records a call
window so 300/900/604800-second TTL behavior can be checked without a flaky
wall-clock estimate. The oracle validates public structured/text projections,
SQLite integrity and foreign keys, schema identity, relational IDs, Git fsck
and cleanliness, archive filename/frontmatter/copy/thread derivation, signal
recipients, token non-disclosure, and receipt idempotency before normalizing
absolute clock values. Timestamp normalization preserves chronological order
and equality classes rather than replacing every timestamp with one wildcard.

The versioned divergence manifest is packaged into wheel and sdist and is
validated against the live fixture and Core source. It permits only the exact
tools/concrete resources/resource templates/prompts publication surfaces of
live 40/0/21/0 versus Core 24/0/0/0, renamed/isolation defaults, provenance and
lazy-LLM boundary, and the three roster-resource description rewrites. The
manifest's single `product_decisions` array is the normative decision ledger.
Every entry independently records selection, implementation, and cutover state,
so a selected design cannot be mistaken for implemented or cutover-approved
behavior. This document deliberately does not duplicate entry scopes.
Unselected and selected-but-unimplemented entries retain
`comparator_disposition: fail`; implemented selections are not allowances and
must assert their selected behavior. The current ledger records the approved
authority-cutover selections as `go`; a separately scoped post-cutover
follow-up may remain `no_go` without reversing that approval.

## Decision material

- [Product decision packet](agentstack-mail-decision-packet.md) records the
  observed live/Core edge behavior, incompatible goals, option impacts, and
  post-decision tests. Normative selections remain in the manifest rather than
  this evidence packet.
- [Claim/enrollment design](agentstack-mail-claim-enrollment-design.md) frames
  credential issuance, legacy null-token ownership proof, recovery, macro
  integration, migration, and rollback implications; normative selections stay
  in the manifest ledger.
- [Performance gate design](agentstack-mail-performance-gate.md) specifies the
  separate measurement boundary needed to close the timing-normalization blind
  spot. It is a design, not an implemented budget or release gate.
