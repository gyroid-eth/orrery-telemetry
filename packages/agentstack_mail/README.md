# agentstack-mail

This subtree contains the contract, provenance, and first bootable core for an
AgentStack-owned coordination mail service. It is not a production server yet.

The implementation is a semantic extraction from the live Python AgentMail
checkout that AgentStack currently uses. It preserves the selected public tool
contracts and existing data model while moving the Python namespace, provider
identity, port, database, archive, signal directory, and service labels into
an AgentStack-owned namespace. The client compatibility keys stay Claude
`mcp-agent-mail` and Codex `agent-mail` during the initial cutover.

Contract v1 contains 24 upstream tools: 12 required by executable AgentStack
runtime paths, 23 visible through shipped model permissions or the delegate
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
exactly the 24 compatibility tools, zero concrete resources, zero resource
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
database, the legacy archive working tree without its `.git` or `server.pid`,
and the signal tree into one sibling staging generation. It holds SQLite's
writer slot with `BEGIN IMMEDIATE` on a `mode=rw` connection, then marks that
guard connection query-only for the full copy. It compares logical rows and
relationship projections, creates one unrelated root commit whose tree exactly
matches the copied archive, and publishes all three surfaces with one directory
rename. Read-only WAL opens may create `-wal`/`-shm`; closing the deliberately
read-write writer guard may additionally checkpoint or remove them and change
main-file bytes. The manifest's `database_policy` therefore excludes sidecar
presence and bytes and compares the main database's schema, rows,
relationships, and PRAGMAs instead. The guard is retained because an external
process check cannot prevent a writer from appearing immediately after the
check. The cutover procedure requires a byte-exact cold copy before this cost
is incurred. The migration does not perform its own sidecar cleanup.

`cold-backup` is the machine implementation of that pre-open copy. With both
authorities externally stopped, it fixes the main/WAL/SHM presence set,
copies each present regular single-link file into a sibling staging directory,
fsyncs the raw files, validates only a disposable clone through SQLite, writes
a canonical receipt, and atomically publishes and fsyncs the backup directory.
`cold-restore` requires that receipt plus the verified C3 migration manifest.
It rejects path aliases and false target classifications, requires an observed
file-generation difference for a rehearsal, atomically replaces `PRESENT`
files, quarantines sidecars recorded `ABSENT`, and writes its terminal receipt
only after raw and logical post-restore validation, cleanup, and parent fsync.
The services-stopped flag remains an explicit caller assertion; neither command
controls launchd or a running service.

`scripts/build_rehearsal_seed.py` builds the reproducible non-production input
for restore acceptance. It never opens the recorded production path. Instead,
it creates a deterministic 50+ MiB SQLite database with 800 agents, 8,200
messages and recipients, a minimal archive and signal tree, and exact synthetic
provenance. The executing generator and `migration.py` bytes must match a clean,
exact candidate commit. The seed, provenance, and write-once generator receipt
are fsynced in one sibling staging generation before atomic directory publish.
Caller-authored `production-read-only-clone` provenance is rejected until a
separate capture command can produce independently bound acquisition evidence.

`cold-restore-rehearse` consumes that seed and its verified migration manifest
without opening the production database. It retains source, backup, damaged,
and restored raw main/WAL/SHM families under one UUID, applies a built-in
physical and logical non-no-op fault, exercises `PRESENT` replacement and an
originally `ABSENT` sidecar removal, then publishes a candidate-bound terminal
receipt. `cold-restore-rehearsal-verify` requires out-of-band receipt SHA, run
UUID, and candidate pins before it re-hashes and logically reopens the retained
artifacts and creates a separate write-once verifier receipt. A subsequent
`--check-only` call additionally requires the verifier-receipt SHA and repeats
the computation without changing either receipt or the evidence tree.

Rehearsal success is only the canonical receipt after an observed zero exit and
separate verification. A `.prepared` or `.unconfirmed` receipt, an ownership
marker without the canonical receipt, or a nonzero/unknown command result is an
incident, not success; those artifacts must not be manually renamed. Receipt
text cannot prove that storage actually flushed or that an atomic syscall ran.
Those properties are covered by the implementation and injected-I/O failure
tests, while the receipts prove only what can be recomputed from retained raw
artifacts.

The release sequence is therefore: generate the seed, create the rehearsal
receipt, publish one verifier receipt, then perform a read-only recheck with
all three generator, rehearsal, and verifier receipt hashes pinned outside the
run directory. The complete asserted
commands and paths are in `docs/agentstack-mail-cutover.md`; the CLI surfaces
are:

```text
python packages/agentstack_mail/scripts/build_rehearsal_seed.py \
  --output-root ABSENT_ROOT --production-source-db PRODUCTION_PATH \
  --candidate-repo CLEAN_REPO --candidate-commit FULL_SHA
agentstack-mail-migrate cold-restore-rehearse \
  --seed-db SEED --production-source-db PRODUCTION_PATH --run-dir ABSENT_RUN \
  --migration-manifest MANIFEST --candidate-repo CLEAN_REPO \
  --candidate-commit FULL_SHA --seed-provenance PROVENANCE \
  --generator-receipt GENERATOR_RECEIPT \
  --expected-generator-receipt-sha256 GENERATOR_RECEIPT_SHA
agentstack-mail-migrate cold-restore-rehearsal-verify \
  --receipt RECEIPT --verification-receipt VERIFIER_RECEIPT \
  --expected-receipt-sha256 RECEIPT_SHA --expected-run-id RUN_UUID \
  --expected-candidate-commit FULL_SHA
agentstack-mail-migrate cold-restore-rehearsal-verify --check-only \
  --receipt RECEIPT --verification-receipt VERIFIER_RECEIPT \
  --expected-receipt-sha256 RECEIPT_SHA \
  --expected-verification-receipt-sha256 VERIFIER_SHA \
  --expected-run-id RUN_UUID --expected-candidate-commit FULL_SHA
```

Identical state is a write-free no-op; an active SQLite writer, corrupt input,
source mutation, aliases or hard links, a different existing destination,
unreachable destination Git objects, and pre-publication I/O failures fail
closed. An interruption after atomic publish leaves a complete marker-owned
generation that must be reverified and finalized by rerunning `copy`; the
foreground service refuses that marker. Destination absence is rechecked
immediately before publish. File descriptors, inode/link checks, and
parent-directory change detection close the observed substitution races, but
non-cooperating same-UID filesystem mutation is still outside the documented
single-operator model; the final destination check/rename has no `RENAME_EXCL`
guarantee. `verify` and `rollback-assess` take one-transaction logical read
snapshots without acquiring the copy-only writer fence. Rollback reports
post-baseline writes after client switching as `no_go` because no verified
reverse transform exists. The sole canonical
`C6_NEW_AUTHORITY_VERIFIED` spelling is unconditionally `no_go`, even when
files still equal the baseline, because the caller-asserted C6 stage is at or
beyond the durable-write boundary and the initial cutover is fix-forward-only.
Aliases such as `C6_CUTOVER_COMPLETE` fail at argparse. A separately
implemented, rehearsed, and approved post-authority reverse transform remains a
post-cutover task rather than an initial-cutover rollback claim.

`agentstack-mail-service` renders a content-aware launchd plist and ownership
manifest without registering it. Its explicit start/stop controller refuses
unknown definitions, compensates a partial bootstrap, and allows a proven-owned
job to be stopped even if its environment file drifted. `status: job_loaded`
proves only the exact launchd definition, not HTTP/MCP readiness. Its foreground
supervisor owns a child process group and holds one authority lock for the
canonical new state root. This artifact is macOS launchd-only; systemd and
`restart` are outside this slice. The cutover installer, approved live-consumer
inventory and predeployment, legacy/new cross-product writer exclusion, and
post-authority reverse migration remain later work. The canonical release
manifest therefore continues to mark the overall service-lifecycle, migration,
and rollback gates `not_implemented`; these tools alone are not release-ready.
The normal cutover must not cross its documented first-write boundary until
those gaps are resolved.

The service label defaults to the production-compatible
`org.orrery.mail`. An explicit custom `--label` is accepted only below
`org.orrery.mail.rehearsal.` with a bounded lowercase ASCII suffix. Render,
ownership, status, start, and stop all use the same selected label; an
ownership/CLI mismatch fails before `launchctl` is called. A rehearsal must
also call the read-only absence preflight before render/start: the production
label, an unreserved label, an existing job, or an unknown manager result all
fail closed. The custom label does not weaken endpoint or state-root isolation,
and omitting `--label` preserves the original artifact names and controller
identity exactly.

Port 8765 is permitted by the pure environment validator so an operator can
keep existing MCP client URLs unchanged. It is not permitted unconditionally
at service start. A same-port start requires the exact `/api/` path and a
configured `AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL`; the product does not embed a
machine-specific legacy label. `start` rejects a loaded legacy job, an unknown
legacy job state, or any 8765 listener that is not a direct child of the exact
owned new launchd wrapper. Its error tells the operator to boot out the legacy
job and verify the port is free. Port 18765 remains available for isolated
rehearsals. `status: job_loaded` is still not MCP readiness: the production
runbook probes the distributed `http://127.0.0.1:8765/api/` path and expected
database before any client is allowed to write.

The HTTP boundary pins Uvicorn 0.52.1 because its graceful SIGTERM workaround
depends on that version's signal-capture behavior. Runtime startup rejects a
different reported version before opening the listener. SIGTERM is suppressed
only after Uvicorn records it so FastMCP's outer database lifespan can finish;
SIGINT remains the conventional exit 130 path. Any pin change therefore
requires the version-drift negative control and lifecycle evidence to be rerun.

`agentstack-mail-consumers` is the separate settings cutover helper. Its
`prepare` command accepts only an explicit typed JSON inventory and writes a
private sealed bundle of exact before/after images without changing any
consumer. `preview` reports only file paths and changed line ranges; it does
not print values or bearer material. `apply`, `status`, and `rollback` require
the externally pinned manifest SHA-256. Whole-set compare-and-swap, immutable
blobs, deterministic crash-resumable stages, same-directory atomic
replacements, and write-once terminal receipts make an interrupted multi-file
switch recoverable with one `rollback` command. The mutable journal is only a
progress diagnostic and cannot authorize a terminal state. Rollback also
requires the verified migration manifest, acquires the same stable authority
lock used by the service, and rechecks that the new authority still equals its
baseline before publication. Exact mode, owner, flags, and
extended attributes are preserved; ACL-bearing inputs are rejected rather
than flattened. The install receipt is rewritten so a later purge cannot
delete the cold legacy roots.

No cross-directory filesystem transaction exists: a crash can leave a mixed
before/after vector, but it is never marked committed, and consumers must
remain quiesced until `status=committed` or `status=rolled_back`. Unknown
aliases, duplicate JSON keys/tables, unsupported formatting, symlinks,
hardlinks, external edits, and bundle tampering fail closed. Stable
target-directory locks serialize cooperating helper processes across file
replacement. A malicious process under the
same UID can still rewrite 0400 bundle data or race the final classify/rename;
that is outside this helper's threat model, so the cutover still requires a
single operator and no other consumer-config writer. The tool validates only
the explicitly listed inventory and is not a filesystem scanner. It does not
rewrite runtime source; Orrery and dashboard compatibility are explicit
pre-cutover prerequisites in the runbook.

`agentstack-mail-consumer-inventory` is the read-only file-discovery stage
that feeds that explicit inventory. It requires the literal `--hidden` and
`--no-ignore` flags, walks every declared root without consulting Git ignore
rules, and publishes nothing unless two independent controls pass: a declared
known-positive selector is present and a declared normally ignored path is in
the matched set. One bounded run seals start/end timestamps, every matched
absolute path, per-file digest and metadata, counts by kind, the generated
inventory digest, and both control results in a private 0700 directory with
0400 artifacts. The collector never prints file contents or selector text.
Its rule spec, exclusions, and controls are explicit and reviewable; this is
not permission to treat a partial rule set as a complete live inventory.
Dynamic tmux/session/reservation capture and operator approval remain separate
cutover gates.

`authorization.py` is the machine-readable inventory for the exact 24-tool
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
24-tool union across identity/contact/message/receipt/search/thread-summary, reservation/signal, and
macro/lifecycle behavior. Every checkpoint compares the public MCP
serialization and durable SQLite, archive, signal, and Git state after raw
integrity, relationship, TTL, receipt-idempotency, archive-derivation, and
credential-leak checks.

Expected differences are fail-closed in
`fixtures/differential-expected-divergences-v2.json`. The only tool-description
allowances are `whois`, `send_message`, and `request_contact`; the live 40-tool
surface versus Core 24-tool surface is pinned across all four MCP publication
axes: tools/concrete resources/resource templates/prompts are live 40/0/21/0
and Core 24/0/0/0. Service namespace/default isolation is also an exact,
versioned allowance. The provider identity remains `agentstack-mail`, while
first cutover preserves Claude's `mcp-agent-mail` and Codex's `agent-mail`
client keys. Authority is validated from endpoint, data roots, and ownership;
client-visible keys are compatibility ABI rather than authority selectors. The
manifest's single `product_decisions` ledger keeps
`decision_state`, `implementation_state`, and `cutover_state` independent and
mandatory. A choice can therefore be selected without pretending it is
implemented or approved for cutover. Unselected and selected-but-unimplemented
decisions remain fail-closed; implemented selections are not allowlisted
differences and must pass their exact selected-behavior tests.

## Cutover readiness

The same manifest now carries a versioned `cutover_gate`. Its result is a
read-only computation, not an approval record: the evaluator never updates a
decision, creates an authority file, starts or stops a service, changes a
client, or performs migration. `go` is possible only when the exact 14-condition
registry is present and every condition passes. D1-D6 and D8-D12 must already
contain the operator's explicit `cutover_state: go`; D7 must remain the exact
selected, unimplemented, post-cutover deferral recorded as `no_go`. Every
pre-cutover task must be implemented and backed by its named machine gate.
Missing, duplicate, extra, malformed, skipped, failed, or unknown inputs all
produce `no_go`.

The former 19-task pre-cutover set is split by actual cutover harm: seven tasks
remain pre-cutover and twelve broad diagnostics, performance, CI, installer,
soak, provenance, and documentation tasks are non-blocking post-cutover work.
The post-authority reverse transform is retained as a thirteenth post-cutover
record rather than being silently discarded. Optional client-key rename and
stale-selector cleanup is a fourteenth post-cutover record; first cutover does
not rewrite permission or hook selectors. Six activation-bound environment
follow-ups bring the non-blocking post-cutover list to 20. Approved-base
persistent-ref reachability is a current gate activation requirement, while the
reservation performance producer/verifier mismatch is a separate all-environment
contract defect. Minimum wheel install/start, root and writer separation, and
normative command/rollback requirements were moved into the seven narrow
pre-cutover contracts before the broad tasks were deferred.

The interim dashboard EXIT boundary is separate from the deferred D7 owner
model: because the HTTP service is loopback-only, `retire_agent` accepts a
token-bearing target without its `registration_token` and records a structured
authorization event. The schema field is retained so a project-administrator
credential can replace this local-process boundary after cutover. This is an
intentional MCP-level difference from frozen live even though it preserves the
live dashboard's end-to-end behavior.

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
three ledger-only conditions. That is 10 or 11 missing conditions,
respectively. The returned `missing_conditions` array is the
canonical remaining-task list: the candidate-source binding itself when the
checkout is dirty or mismatched; decision cutover approvals; candidate-bound
behavior and distribution evidence; reservation-safety evidence; HTTP/CLI
transport; service supervision; MCP client re-registration; data migration and
reconciliation; pre-write rollback; and notification-layout consumer
compatibility.

Machine results are supplied separately with `--evidence`; they are not stored
as mutable verdicts in the ledger. Evidence schema v1 binds raw artifacts to
the exact candidate commit, manifest bytes, and canonical condition definition.
Condition-specific handlers recompute the result from exact pytest node
outcomes, wheel and source-distribution contents, or the required adverse
safety controls. Caller-authored `status`, `passed`,
`verdict`, and generic exit-code claims are invalid. Tasks whose current
evidence kind is `unimplemented_v1` cannot pass at all; implementing a task must
also add and review a versioned raw-artifact handler. Exit codes are 0 for
`go`, 1 for a valid `no_go`, and 2 for invalid ledger or evidence. This guards
accidental and single-field falsification. Raw artifact hashes prove integrity,
not producer identity. For the local first cutover, ProOpus and maintainer inspect
the retained raw artifacts and exact candidate directly; protected-CI or
cryptographic producer and operator attestations remain a versioned
post-cutover hardening task with an explicit activation condition.

The production-shaped restore rehearsal bundle is only a future input to
`data-migration-reconciliation`; the narrow pre-write
`rollback-revert-procedure` requires a separate handler and evidence record.
In v1 both remain `unimplemented_v1`, so the runbook stops before readiness
without writing either record. Reconciliation after durable new-authority
writes is a distinct non-blocking post-cutover reverse-transform task.

The candidate wheel/source-distribution gate also does not bind the transitive
dependency closure. Production venv creation remains NO-GO until an
interpreter-bound, hash-locked offline wheelhouse and install receipt are
versioned evidence; the current runbook install block is future-only.

### Candidate-bound runtime rehearsal

`agentstack-mail-evidence runtime-rehearsal` is the isolated runtime evidence
producer. It must itself run from an installed candidate wheel, byte-compares
every installed `agentstack_mail` member with that wheel, requires the named
clean candidate commit to equal checkout `HEAD`, and requires its own source
blob to match the same commit. The output directory must not exist. Receipts
are written once with mode 0400; an interrupted run retains its in-progress
marker and cannot be mistaken for a terminal result.

```console
agentstack-mail-evidence runtime-rehearsal \
  --output-dir /absolute/absent/evidence-directory \
  --wheel /absolute/candidate/agentstack_mail-0.0.0-py3-none-any.whl \
  --candidate-repo /absolute/clean/candidate-checkout \
  --candidate-commit 0123456789abcdef0123456789abcdef01234567 \
  --port 18765
```

The producer uses only the specified isolated port and roots. It observes the
legacy 8765 listener with a read-only `lsof` table and sends that listener zero
network requests. It exercises both installed entrypoints, the exact 24-tool
surface, normal stop/restart, duplicate rejection, child crash/recovery, and
the wrapper-SIGKILL case where the surviving server must retain the authority
lock. Any failed run stops every process it spawned and then removes only a
listener on the explicitly selected isolated port. The
`--allow-missing-legacy-listener` option is for an offline test machine, not a
cutover receipt.

After the foreground producer has emitted a same-candidate lifecycle receipt,
`launchd-rehearsal` can exercise the installed service controller under one
operator-authorized, non-production launchd identity:

```console
agentstack-mail-evidence launchd-rehearsal \
  --output-dir /absolute/absent/launchd-evidence-directory \
  --wheel /absolute/candidate/agentstack_mail-0.0.0-py3-none-any.whl \
  --candidate-repo /absolute/clean/candidate-checkout \
  --candidate-commit 0123456789abcdef0123456789abcdef01234567 \
  --foreground-receipt /absolute/service-lifecycle-v1.json \
  --foreground-receipt-sha256 "$FOREGROUND_RECEIPT_SHA256" \
  --label org.orrery.mail.rehearsal.01234567.one-use-nonce \
  --port 28765
```

Before the legacy job is stopped, `legacy-launchd-snapshot` seals the exact
rollback definition while sending the legacy endpoint zero network requests:

```console
agentstack-mail-evidence legacy-launchd-snapshot \
  --output /absolute/absent/legacy-launchd-definition-v1.json \
  --wheel /absolute/candidate/agentstack_mail-0.0.0-py3-none-any.whl \
  --candidate-repo /absolute/clean/candidate-checkout \
  --candidate-commit 0123456789abcdef0123456789abcdef01234567
```

The producer is restricted to the loaded `com.operator.mcp-agent-mail` job. It
byte-binds itself and the installed package to the named wheel and clean
candidate, checks the loaded path/program/arguments against the plist, retains
the complete plist bytes as base64 plus its digest and
`KeepAlive`/`RunAtLoad`/working directory, and proves the
single 8765 listener is a child of the loaded wrapper. The new
`org.orrery.mail` identity must still be absent. The write-once receipt has
mode 0400; a missing or foreign job, topology drift, or an existing output
fails closed. Complete bytes are accepted only for the exact legacy label and
an allowlisted plist shape; environment keys other than `HOME` and `PATH` are
rejected rather than copied into the receipt.

The label must contain the exact candidate's first eight hexadecimal digits.
This producer may change launchd state only through the installed controller's
`bootstrap`, `enable`, `kickstart`, and `bootout` calls for that exact label.
It reads, but does not change, the production launchd identity and legacy 8765
listener. It publishes no terminal receipt unless the rehearsal label is
absent again, the isolated port is closed, and both production observations
are unchanged. The receipt retains only the exact test label's disabled
override value; it never stores the full `print-disabled` domain output.

`launchctl bootstrap` EIO is not treated as proof that no job was loaded. The
controller immediately re-reads the exact label: only the same owned
path/program/arguments may continue to `enable` and `kickstart`, without a
second bootstrap. The result records the rc-113 preflight separately from the
post-EIO recheck. Absent, foreign, or unknown post-EIO state stops before any
further mutation. Cleanup likewise polls an exact owned label until launchd's
asynchronous `bootout` reaches rc 113; ownership drift fails immediately.

On this Mac, state-changing `launchctl` calls from the Codex sandbox return
EIO while the same new label and plist succeed from the operator shell. The
entire installed `launchd-rehearsal` command—not isolated launchctl fragments
and not a hand-authored receipt—must therefore be run by the authorized
operator outside that sandbox. Its terminal receipt is then verified
read-only against the external SHA-256 pin.

This producer does not install its own environment and does not claim that a
dependency closure is hash-locked. Therefore adding it alone does not change
either `http-cli-transport-entrypoints` or
`service-lifecycle-supervision` to implemented; the canonical manifest remains
NO-GO until the relevant reviewed evidence handler and all named prerequisites
exist.

This checked-in snapshot deliberately returns `no_go`: reservation safety is
implemented but still lacks final-candidate raw evidence, the other six narrow
pre-cutover tasks remain `not_implemented`, and current decision approvals are
`no_go`. A task becomes satisfiable only in a reviewed change that updates its
implementation state, introduces its versioned raw handler, and updates the
canonical manifest pin together. The first ready candidate must add an
end-to-end fixture that reaches `go` through both the canonical artifact
validator and the readiness evaluator; a synthetic unpinned manifest is not
sufficient.

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

The 24-tool contract does not expose an MCP roster resource. Callers obtain
their own assigned identity from the AgentStack runtime, `register_agent`, or
`macro_start_session`; `list_contacts` returns known contact links and `whois`
verifies a known name. `send_message(..., to=[], broadcast=true)` is the
roster-free broadcast path. Enabling an upstream subset tool-filter profile
fails server construction instead of weakening the exact contract.

See `NOTICE.md` for the exact source baseline. The checked-in live tool-schema
fixture is evidence, not an instruction to expose every upstream tool.
