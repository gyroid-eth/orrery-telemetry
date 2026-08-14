# AgentStack mail extraction

`agentstack-mail` is developed inside this repository as a logically isolated
package first. Repository extraction is deliberately deferred until the
versioned contract, independent export/test gate, install/upgrade/rollback
manifest, and N/N-1 consumer tests are stable.

The authoritative implementation input is the working live Python AgentMail
checkout, including its local commits and dirty signal/runtime fixes. Current
upstream is retained only as an advisory security and bug-fix source.

The development endpoint is `http://127.0.0.1:18765`; data lives below
`~/.agentstack/mail`. Existing AgentMail remains on its own endpoint and data
roots throughout development. No test or installer may point both services at
one writable database or archive.

The caller-derived compatibility surface is versioned in
`packages/agentstack_mail/fixtures/compatibility-tools-v1.json`. Its 24 tools
are the positive union of executable callers and shipped model-facing
contracts. Permission deny entries, negative instructions, and Codex
Bridge-local operations do not become source-extraction roots.

The first implementation train is:

1. freeze provenance, live tool schemas, and the caller-derived tool contract;
2. define isolated configuration and an exact-schema database copy/import gate;
3. port identity, messaging/contact, receipt, reservation, and notification
   behavior with differential tests against the live source;
4. port HTTP and lifecycle stability without the machine-specific notify and
   tmux daemons;
5. update installer, doctor, bridge, and hooks atomically to the new endpoint
   and authentication while preserving each client's existing MCP key;
6. run coexistence, migration, rollback, fault, and real-machine soak gates
   before any authority switch.

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
commands are implemented. Their clean candidate-bound release evidence and the
installer/client authority switch are not complete, so the corresponding
cutover conditions remain `no_go`.

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
must assert their selected behavior. Every current entry has
`cutover_state: no_go`, so the gate does not authorize authority cutover.

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
