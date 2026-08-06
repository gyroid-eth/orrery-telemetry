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

All production settings use the `AGENTSTACK_MAIL_*` namespace. With no new
settings present, the resolved port is `18765` and database, archive, and
signals are below `~/.agentstack/mail`; legacy unprefixed variables and a CWD
`.env` are ignored. The package intentionally has no HTTP/CLI entrypoint,
supervisor, migration command, or installer switch yet.

The behavior train must explicitly decide the owner-token, failed-registration
atomicity, contact expiry, cross-project reply identity, DB-to-Git recovery,
receipt atomicity, and signal-cleanup semantics before authority can move.
