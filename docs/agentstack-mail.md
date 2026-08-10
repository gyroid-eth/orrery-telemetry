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
`packages/agentstack_mail/fixtures/compatibility-tools-v1.json`. Its 22 tools
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
5. update installer, doctor, bridge, hooks, skills, and permissions atomically
   to the new MCP key;
6. run coexistence, migration, rollback, fault, and real-machine soak gates
   before any authority switch.

The old `mcp-agent-mail` MCP key is not registered as an alias by default,
because doing so would recreate the collision this package is intended to
remove. Existing record compatibility is a data/schema requirement, separate
from tool-prefix compatibility.

## Current core boundary

The core train copies the live data/archive/tool-body seam into the renamed
package and publishes exactly the 22 versioned tools through a fail-closed
FastMCP subclass. MCP resources and the 18 non-compatibility tools are not
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
`.env` are ignored. The package intentionally has no HTTP/CLI entrypoint,
supervisor, migration command, or installer switch yet.

File-reservation activity probes converge on upstream #240's one-pathspec Git
walk, then add a process-global concurrency limit of eight, a three-second
per-probe deadline, and a four-second status-pass budget. A timed-out, failed,
or incomplete filesystem/Git probe is explicit unknown activity and therefore
cannot trigger stale auto-release; TTL expiry is unchanged. The package-local
performance gate repeats 57 concrete tracked paths five times, requires a
six-second-or-better median and at least three fully matched/complete runs, and
reports the maximum separately. Fingerprints exclude mutable activity
timestamps.

The provenance Git bundle and dirty patch remain repository-only audit inputs
and are excluded from wheels and source distributions. Distribution gates
verify both artifact types still contain the runtime modules, NOTICE, both
licenses, and the versioned fixtures.

## Behavior differential gate

The approved Core base is `de625ed`. Behavior tests authenticate and reconstruct
the frozen live baseline from the checked-in Git bundle and dirty patch, then
start live and Core in separate subprocesses. Worker environments inherit only
an OS bootstrap allowlist; database, archive, signals, home, temporary files,
Git identity, port, and import roots are explicitly isolated. Test inputs and
outputs are private, symlink escape and source-origin drift fail closed, and no
developer AgentMail checkout is consulted.

The ordered scenarios are:

1. identity, contact, messaging, topic/inbox, mark-read, acknowledgement replay,
   and reply;
2. Unicode reservation idempotency/conflict/renew/release plus per-message
   signals and BCC privacy;
3. health, start-session, reservation-cycle, contact-handshake, summary fetch,
   and retirement lifecycle.

Their union is exactly the versioned 22 tools. Each operation records a call
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
live 40/0/21/0 versus Core 22/0/0/0, renamed/isolation defaults, provenance and
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
