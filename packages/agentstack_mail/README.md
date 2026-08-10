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
exactly the 22 compatibility tools, zero concrete resources, zero resource
templates, and zero prompts. Non-compatibility bodies are retained internally
only until the differential suite proves they can be
removed. The installed `agentstack-mail` console script serves this boundary at
the loopback-only default `http://127.0.0.1:18765/mcp`; it rejects non-loopback
binds and bearer/JWT settings until the HTTP authentication layer is wired.
`agentstack-mail --help` prints usage without starting the server, and
`--host`, `--port`, and `--path` override the endpoint for one process. Naming
mode behavior remains compatible with the frozen source, including default
`coerce` substitution. Fixed AgentStack identities therefore use
`AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough`; the unprefixed live
key is intentionally not a fallback. AgentStack's Claude registration hook
checks explicit request and response names before marking registration success;
Codex launch paths have separate, partial comparison coverage documented in the
cutover procedure.

The separate `agentstack-mail-migrate` console copies a quiesced SQLite
database, Git archive, and signal tree into one sibling staging generation,
compares full logical rows and relationship projections, and publishes the
three surfaces with one directory rename. Identical state is a write-free
no-op; corrupt input, source mutation, a different existing destination, and
pre-publication I/O failures fail closed. An interruption after atomic publish
leaves a complete marker-owned generation that must be reverified and finalized
by rerunning `copy`; the foreground service refuses that marker. Destination
absence is rechecked immediately before publish, but the remaining check/rename
TOCTOU is accepted only under the documented single-operator assumption and is
not safe for concurrent migration writers. Its read-only `rollback-assess`
command reports post-baseline writes after client switching as `no_go` because
no verified reverse transform exists.

`agentstack-mail-service` renders a content-aware launchd plist and ownership
manifest without registering it. Its explicit start/stop controller refuses
unknown definitions, compensates a partial bootstrap, and allows a proven-owned
job to be stopped even if its environment file drifted. `status: job_loaded`
proves only the exact launchd definition, not HTTP/MCP readiness. Its foreground
supervisor owns a child process group and holds one authority lock for the
canonical new state root. This artifact is macOS launchd-only; systemd and
`restart` are outside this slice. The cutover installer, transactional
all-consumer switch, legacy/new cross-product writer exclusion, and
post-authority reverse migration remain later work. The canonical release
manifest therefore continues to mark the overall service-lifecycle, migration,
and rollback gates `not_implemented`; these tools alone are not release-ready.
The normal cutover must not cross its documented first-write boundary until
those gaps are resolved.

`authorization.py` is the machine-readable inventory for the exact 22-tool
surface. Each entry records the prospective subject, action, resource, current
required arguments, existing credential arguments, and future authorization
rule without adding credentials to any MCP schema. The runtime emits a
four-field, credential-free shadow observation (`principal_candidate`, `tool`,
`decision`, `reason`) for each valid invocation that reaches the observer. No
configured policy means default allow with zero `would_deny` observations, and
shadow verdicts are discarded rather than enforced. A synthetic deny is
therefore observable while the underlying operation still succeeds. Server
construction fails closed if the runtime catalog, canonical versioned fixture,
or published tool set diverges.

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
`fixtures/differential-expected-divergences-v2.json`. The only tool-description
allowances are `whois`, `send_message`, and `request_contact`; the live 40-tool
surface versus Core 22-tool surface is pinned across all four MCP publication
axes: tools/concrete resources/resource templates/prompts are live 40/0/21/0
and Core 22/0/0/0. Service namespace/default isolation is also an exact,
versioned allowance. The manifest's single `product_decisions` ledger keeps
`decision_state`, `implementation_state`, and `cutover_state` independent and
mandatory. A choice can therefore be selected without pretending it is
implemented or approved for cutover. Unselected and selected-but-unimplemented
decisions remain fail-closed; implemented selections are not allowlisted
differences and must pass their exact selected-behavior tests.

## Cutover readiness

The same manifest now carries a versioned `cutover_gate`. Its result is a
read-only computation, not an approval record: the evaluator never updates a
decision, creates an authority file, starts or stops a service, changes a
client, or performs migration. `go` is possible only when the exact 26-condition
registry is present and every condition passes. D1-D6 and D8-D12 must already
contain the operator's explicit `cutover_state: go`; D7 must remain the exact
selected, unimplemented, post-cutover deferral recorded as `no_go`. Every
pre-cutover task must be implemented and backed by its named machine gate.
Missing, duplicate, extra, malformed, skipped, failed, or unknown inputs all
produce `no_go`.

Run the current ledger check from the repository root:

```console
python3 packages/agentstack_mail/tests/cutover_readiness.py \
  --candidate-commit 0123456789abcdef0123456789abcdef01234567
```

Replace the example with the explicitly designated full commit; symbolic refs
such as `HEAD` are rejected. The designated commit must equal the evaluator
checkout's `HEAD`, the tracked and untracked worktree status must be clean, and
the evaluator repeats both observations before returning.

The checked-in ledger intentionally returns exit 1 and `cutover_state: no_go`.
In a clean checkout whose explicit full candidate commit equals `HEAD`, four
conditions pass without external evidence; a dirty checkout passes only the
three ledger-only conditions. That is 22 or 23 missing conditions,
respectively. The returned `missing_conditions` array is the
canonical remaining-task list: the candidate-source binding itself when the
checkout is dirty or mismatched; decision cutover approvals; candidate-bound
behavior, distribution, reservation-safety, and reservation-performance
implementation and evidence; three timeout-diagnostic tasks; stale provenance
regression assertions; HTTP/CLI transport; service supervision; installer
integration; MCP client re-registration; data migration and reconciliation;
rollback; coexistence/fault/soak gates; the full performance/load/soak matrix;
notification-layout consumer compatibility; full-repository and installed-wheel
release gates; trusted evidence and operator-approval provenance; and
cutover-document consistency.

Machine results are supplied separately with `--evidence`; they are not stored
as mutable verdicts in the ledger. Evidence schema v1 binds raw artifacts to
the exact candidate commit, manifest bytes, and canonical condition definition.
Condition-specific handlers recompute the result from exact pytest node
outcomes, wheel and source-distribution contents, five-run performance data, or
the required adverse safety controls. Caller-authored `status`, `passed`,
`verdict`, and generic exit-code claims are invalid. Tasks whose current
evidence kind is `unimplemented_v1` cannot pass at all; implementing a task must
also add and review a versioned raw-artifact handler. Exit codes are 0 for
`go`, 1 for a valid `no_go`, and 2 for invalid ledger or evidence. This guards
accidental and single-field falsification. Raw artifact hashes prove integrity,
not producer identity: the independent
`cutover-evidence-provenance-gate` therefore remains a mandatory pre-cutover
task until protected CI or cryptographic attestations bind the producer,
commands, candidate, manifest, condition definitions, artifacts, and operator
approval. Hand-authored or replayed raw JSON is not sufficient for global
`go`.

This v1 snapshot deliberately has no reachable `go`: the canonical artifact
validator pins every listed follow-up task as `not_implemented` and pins the
current decision approvals as `no_go`. A future task may become satisfiable
only in a reviewed change that updates its implementation state, introduces its
versioned raw handler, and updates the canonical manifest pin together. The
first future-ready candidate must add an end-to-end fixture that reaches `go`
through both the canonical artifact validator and the readiness evaluator; a
synthetic unpinned manifest is not sufficient.

Run the focused gate from the repository root with:

```console
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q packages/agentstack_mail/tests/test_differential.py -p no:cacheprovider
```

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

It repeats a deterministic 57-tracked-path sample five times and requires the
median to stay within six seconds, with at least three runs fully matched and
complete. The maximum is reported for diagnostics but does not let a single
loaded-machine outlier fail the gate. The input and result-shape fingerprints
exclude activity timestamps, which change as the workspace is committed. The
configured live-pattern snapshot is also diagnostic only.

The 22-tool contract does not expose an MCP roster resource. Callers obtain
their own assigned identity from the AgentStack runtime, `register_agent`, or
`macro_start_session`; `list_contacts` returns known contact links and `whois`
verifies a known name. `send_message(..., to=[], broadcast=true)` is the
roster-free broadcast path. Enabling an upstream subset tool-filter profile
fails server construction instead of weakening the exact contract.

See `NOTICE.md` for the exact source baseline. The checked-in live tool-schema
fixture is evidence, not an instruction to expose every upstream tool.
