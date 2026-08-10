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

Reservation activity sweeps use the upstream #240 single-pathspec Git walk and
bound probes process-wide to eight concurrent subprocesses. Each probe has a
three-second deadline and each status pass has a four-second wall budget. Git
subprocesses are killed and reaped on timeout, filesystem globs stop only on a
deadline or decisive recent mtime, and an incomplete/error result is reported
as unknown and cannot auto-release a reservation. Explicit TTL expiry remains
authoritative. The real-workspace gate is:

```sh
uv run --project packages/agentstack_mail \
  python packages/agentstack_mail/scripts/reservation_performance_gate.py \
  /path/to/workspace
```

It requires a deterministic 57-tracked-path sample to finish completely within
three seconds and prints the configured live-pattern snapshot separately for
diagnostics.

The 22-tool contract does not expose an MCP roster resource. Callers obtain
their own assigned identity from the AgentStack runtime, `register_agent`, or
`macro_start_session`; `list_contacts` returns known contact links and `whois`
verifies a known name. `send_message(..., to=[], broadcast=true)` is the
roster-free broadcast path. Enabling an upstream subset tool-filter profile
fails server construction instead of weakening the exact contract.

See `NOTICE.md` for the exact source baseline. The checked-in live tool-schema
fixture is evidence, not an instruction to expose every upstream tool.
