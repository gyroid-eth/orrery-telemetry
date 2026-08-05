# agentstack-mail

This subtree is the contract and provenance boundary for an AgentStack-owned
coordination mail service. It is not a production server yet.

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

See `NOTICE.md` for the exact source baseline. The checked-in live tool-schema
fixture is evidence, not an instruction to expose every upstream tool.
