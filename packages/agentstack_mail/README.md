# agentstack-mail

This subtree contains the contract, provenance, and first bootable core for an
AgentStack-owned coordination mail service. It is not a production server yet.

The implementation will be a semantic extraction from the live Python
AgentMail checkout that AgentStack currently uses. It will preserve the
selected public tool contracts and existing data model while moving the Python
namespace, MCP server key, port, database, archive, signal directory, and
service labels into an AgentStack-owned namespace.

Contract v1 contains 22 upstream tools: 12 required by executable AgentStack
runtime paths, 21 visible through shipped model permissions or the delegate
skill, and `retire_agent` as the one runtime-only addition. Bridge-local names
such as `runtime_status` are not upstream tools, and `create_agent_identity` is
explicitly excluded.

The first release must satisfy these invariants:

- an existing AgentMail service can keep running on its original endpoint;
- old and new services never write the same database or archive concurrently;
- migration uses a copied database/archive and verifies identity, message,
  recipient, receipt, and reservation semantics before client cutover;
- canonical notification writes use one file per message, while consumers may
  continue to read the legacy single-file layout;
- imported records keep their stable database identifiers and timestamps;
- new AgentStack code retains the repository's PolyForm license while derived
  portions retain the original license and copyright notice;
- every wheel and source distribution carries both license texts and the
  versioned compatibility fixtures.

The reconstructible live Git bundle and dirty patch under `provenance/` are
repository-only audit inputs. They are intentionally excluded from both wheel
and source distributions; the package distributions retain `NOTICE.md`, both
license texts, the compatibility fixtures, runtime source, and verification
tests.

The current core copies the live data, archive, and tool-body seam so it can be
compared without translating behavior. A fail-closed FastMCP boundary publishes
exactly the 22 compatibility tools and no resources. Non-compatibility bodies
are retained internally only until the differential suite proves they can be
removed; HTTP, CLI, supervisor, migration, and consumer cutover are later
trains.

The Behavior differential reconstructs the frozen live source only from the
authenticated repository bundle plus tracked working-tree patch. Live and Core
then run in separate Python processes with disjoint 0700 state roots, private
inputs/outputs, fixed import origins, equivalent explicit configuration, and no
mutable-checkout or network fallback. Three ordered scenarios cover the exact
22-tool union across identity/contact/message/receipt, reservation/signal, and
macro/lifecycle behavior. Every checkpoint compares the public MCP
serialization and durable SQLite, archive, signal, and Git state after raw
integrity, relationship, TTL, receipt-idempotency, archive-derivation, and
credential-leak checks.

Expected differences are fail-closed in
`fixtures/differential-expected-divergences-v1.json`. The only tool-description
allowances are `whois`, `send_message`, and `request_contact`; the live 40-tool
surface versus Core 22-tool surface and service namespace/default isolation are
also exact, versioned allowances. D1–D9 remain unresolved product decisions,
not accepted behavior differences, so observing one still fails the gate.

Run the focused gate from the repository root with:

```console
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q packages/agentstack_mail/tests/test_differential.py -p no:cacheprovider
```

The 22-tool contract does not expose an MCP roster resource. Callers obtain
their own assigned identity from the AgentStack runtime, `register_agent`, or
`macro_start_session`; `list_contacts` returns known contact links and `whois`
verifies a known name. `send_message(..., to=[], broadcast=true)` is the
roster-free broadcast path. Enabling an upstream subset tool-filter profile
fails server construction instead of weakening the exact contract.

See `NOTICE.md` for the exact source baseline. The checked-in live tool-schema
fixture is evidence, not an instruction to expose every upstream tool.
