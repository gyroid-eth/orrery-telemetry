# AgentStack Mail claim and enrollment design

Status: supporting design for the normative product-decision ledger. D7's
selection, implementation state, and cutover state live only in
`packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json`.
This document does not choose an enrollment, rotation, recovery, principal, or
administrator mechanism; change D6's selected upstream-parity behavior;
implement or enforce D7; implement an
authority cutover; or authorize access to a live service or database. D1's
narrow, selected failure-atomicity rule is an input constraint, not a choice
reopened here.

## One-line summary

A safe path needs secret-free enrollment transport, authority external to a
null-token row, explicit rotation and loss recovery, authenticated macro
bootstrap, and an additive shadowed migration before any future strict D6
behavior or the selected D7 boundary can be enforced.

Throughout this packet, **claim** is an umbrella term for the flows that
establish or restore authority: enrollment, recovery, administrator approval,
binding-backed continuity, or an audited transfer. It is not a separately
selected primitive, endpoint, or proof source.

## D7 rationale and design implications

The manifest ledger is the sole source for D7's exact selected scope. This
document does not restate that decision. It records why the selection needs an
enrollment prerequisite, separate principal/admin design, and lifecycle tests.

Name-only retirement is a timing-selectable receive-denial attack. A later
unretire cannot recover sends rejected while the row was retired. A machine
inventory that currently observes zero affected rows would not turn that fact
into a product guarantee.

Current D7 runtime behavior remains unchanged and shadow verdicts are not
enforced. The ledger records D7 as selected, not implemented, and cutover
no-go. D6 is selected and implemented as pre-existing upstream parity, with
cutover still no-go; that selection preserves omitted-token behavior for Path A
and does not authorize stricter enforcement. The default deployment model
remains one local principal with no authorization enforcement; principal
subdivision is later work.

The 24-tool permission catalog is a prospective, non-binding inventory except
where its D7 row mirrors the normative ledger. In particular, its candidate
`send_message` owner/admin rule does not replace D6's selected upstream-parity
rule, and its other rows do not authorize enforcement.

## New credential issuance and return

### Boundary and current evidence

This packet narrows the decisions that must be made after the differential
observations. It does not turn the non-binding leans below into requirements.
The prevalence of each credential state in existing installations is unknown;
an eventual inventory must be separately authorized and must not export raw
credentials.

The checked-in implementation establishes these constraints:

- `register_agent` accepts a caller-supplied `registration_token`, preserves an
  existing token, or generates a new random token when neither exists.
- `_agent_to_dict` deliberately excludes `registration_token`; the adjacent
  comment records more than 1,554 plaintext appearances in JSONL conversation
  logs when it was previously returned.
- `Agent.registration_token` is nullable and currently stored as an indexed
  plaintext value.
- Frozen live sends all bound arguments except `ctx` to Rich logging. Core now
  redacts credential-suffixed top-level arguments before Rich serialization
  and keeps authorization shadow observations to four credential-free fields.
- Frozen live lets a conflicting D1 re-registration reach identity metadata and
  archive mutation before rejection. Core's selected D1 rule now rejects an
  explicitly conflicting token before durable mutation and uses a conditional
  DB write so concurrent registration against a null-token row has one atomic
  writer. The first-DB-writer result is retained unsafe compatibility
  arbitration, not accepted claim proof. D6's selected upstream parity retains
  omitted-token sending without turning it into owner proof; the selected D7
  boundary is not implemented and does not reinterpret D1's writer result as
  owner authority.
- `macro_start_session` accepts no credential, calls `_get_or_create_agent`
  directly, and can then reserve files and read inbox state for a newly created
  null-token identity.
- D3 dynamically confirms a second new-null path: an approved cross-project
  send calls `_get_or_create_agent` in the target project without token
  management and creates a target-local sender alias whose
  `registration_token` is null. This is measured evidence for D7's existing
  stop-all-new-null prerequisite, not a change to D7's selected boundary.
- Current behavior selected under D6 lets a tokenized sender omit
  `sender_token`. Current Core still conditionally allows name-only owner
  operations when the row token is null; that behavior is not the selected,
  unimplemented D7 boundary.
- D1 result/profile tests do not establish end-to-end secret non-disclosure.
  Core's new Rich redaction closes the known top-level token path, but nested
  aliases, exceptions, and future transport surfaces still need end-to-end
  canary gates.
- Existing launchers already demonstrate useful primitives: caller-generated
  tokens, mode-`0600` files under mode-`0700` directories, atomic replacement,
  stdin rather than literal argv transport, and a proxy that keeps the token
  outside the model-facing tool surface. The Codex App bridge also keeps a
  durable external-ID binding separate from its owner-token file.

These are implementation observations, not approval of the current storage or
launcher design.

An enrollment has two outputs with different confidentiality requirements:

1. The identity result: canonical project/name, credential version, enrollment
   state, and a non-secret receipt or fingerprint.
2. The reusable owner credential: a high-entropy bearer secret or an equivalent
   non-exportable binding.

The ordinary tool result may carry the first output. It must not carry the
second unless the product explicitly chooses the plaintext-return option and
accepts its archive exposure.

### Issuance and delivery options

| Option | Security and operational shape | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| Caller generates a token and passes it as an ordinary `register_agent` argument; server returns only a receipt/fingerprint | Simple and already understood, but the secret enters the model/tool request and current Rich kwargs unless both are redacted | Model-driven callers, traces, and log collectors must become secret-aware; callers that cannot generate and retain a token cannot enroll | Already-known caller tokens remain usable; generated-but-unavailable and null-token rows are not recovered |
| Caller generates a token, while a trusted launcher/proxy injects it from a private file, file descriptor, or authenticated local channel | Keeps the secret off the model-facing schema and result; aligns with existing launcher and Codex App seams | Direct remote MCP clients need an equivalent secure transport; a server must not accept an arbitrary caller-chosen filesystem path | Existing private token files and bridge bindings may be reusable only after ownership and permission checks; no null row is automatically claimed |
| Server generates a token and writes it once to a pre-authorized secret sink, returning a receipt/fingerprint | Server controls entropy and never emits the secret in the ordinary result; success needs an atomic DB/sink protocol | Remote sinks, crash recovery, sink authentication, and retry semantics add a protocol; headless clients without a sink cannot enroll | New enrollments can use it; it cannot reconstruct previously generated unavailable tokens |
| Server returns a generated token once in plaintext | Works for generic clients with no auxiliary channel | Recreates the known JSONL/result leak and requires every intermediary, UI, logger, and transcript store to protect the result | No row migration is required, but old leaked tokens need rotation rather than retrospective protection |
| Server issues a short-lived, single-use enrollment handle that a second trusted channel redeems | Separates identity creation from secret delivery and can expire incomplete enrollment | The handle is itself a temporary bearer secret; new pending state, expiry, replay protection, and a second round trip are required | Existing rows receive no authority merely by being assigned a handle; a separate claim proof is still required |
| A trusted bridge holds a non-exportable process/session binding and authenticates calls without exposing a bearer to the agent | Removes reusable secrets from the model process and already resembles the Codex App proxy | Generic clients, process migration, disaster recovery, and bridge unavailability need separate paths | Only bindings recorded before a disputed claim can provide evidence; current unbound rows need another route |

The choice may be a combination. For example, a proxy can inject a
caller-generated credential locally while a remote client uses a one-time
sink. Combining transports must not produce different ownership semantics.

### Credential verification at rest

Issuance transport does not decide how the server verifies the credential.
That decision also needs explicit compatibility treatment.

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Keep plaintext bearer values | A database read grants immediate impersonation; indexed secret values also expand accidental exposure | Maximum rollback compatibility; all existing values remain as-is |
| Store a versioned one-way verifier plus a non-secret fingerprint | Plaintext recovery becomes impossible by design; diagnostics and clients must rotate rather than retrieve | Existing plaintext values need a dual-read migration; deleting plaintext is irreversible and must occur only after the rollback window |
| Put bearer material in an external secret service and keep only a reference/verifier in the row | Adds availability, deployment, backup, and authority dependencies | Existing rows need an explicit import or remain on the legacy verifier; rollback depends on retaining the legacy representation |

A random bearer token does not need to be decryptable for equality
verification. If a digest is selected, it must be versioned and domain
separated; a server-side keyed verifier reduces the value of an offline table
copy. Exact construction, key custody, and schema are post-decision work.

### Leak-prevention contract

Secret safety is an end-to-end property, not a response-field omission. The
eventual design should treat owner credentials and one-time enrollment handles
as tainted values across these surfaces:

| Surface | Required invariant | Current or residual risk to test |
|---|---|---|
| Tool request and model transcript | The model-facing call uses a credential reference or bound proxy; if a secret-valued compatibility parameter remains, redact it before any instrumentation | Core redacts top-level credential-suffixed fields before Rich logging; frozen live, aliases, nested payloads, and future instrumentation remain canary-test surfaces |
| Tool result and error | No reusable secret in structured content, text content, formatting variants, exception data, or retry hints | `_agent_to_dict` is safe, but future enrollment and macro results could regress |
| Logs and telemetry | Redact by schema/taint before serialization; never interpolate values; avoid traceback locals and request dumps | Field-name-only redaction misses aliases, nested payloads, and formatted JSON strings |
| Messages, profiles, archives, Git, dashboard, and notifications | Store only state, receipt, version, and a fingerprint safe for correlation | A credential sent in mail is already disclosed even if later deleted; Git history is durable |
| Process argv and process listings | Never place a literal credential in argv; pass bytes through a protected file descriptor, stdin, or a local proxy | Paths and identity names may remain visible, but must not be accepted as authority |
| Shell history and terminal transcript | Never require `export TOKEN=...`, literal `curl` arguments, command substitution that prints the secret, or echoed prompts | stdin can still be captured if a wrapper enables tracing; scripts must disable secret echo/xtrace around the boundary |
| Environment | Avoid reusable credentials in inherited or tmux-global environment; if compatibility requires an environment variable, scope it to one process and scrub it before child launch | Crash reports and environment dumps can copy the value |
| Secret files and handoff files | Parent directory `0700`; file `0600`; no-follow and regular-file checks; exclusive create; fsync and atomic install; one-shot source removed only after durable success; never commit or sync | The current child state JSON duplicates the token, increasing copies and cleanup obligations |
| Database and backups | Store the chosen verifier intentionally; omit raw credentials from exports, fixtures, query telemetry, and diagnostics | Current plaintext indexed column and backups remain sensitive until separately migrated |

The result/log/message/argv/history scans are release gates, not best-effort
documentation. A secret-canary test should fail on any unexpected occurrence,
including failure and retry paths.

## Existing null-token authority establishment: proof is the central decision

### What the existing row can and cannot prove

A null-token `Agent` row contains an identity label and mutable operational
metadata, but no owner secret. Consequently, the row cannot by itself prove
which caller owns it. The project key, name, task description, program/model,
timestamps, inbox contents, tmux session name, pane title, hostname, and
`AGENT_NAME` are identifiers or ambient facts, not ownership proof.

`WindowIdentity` does not close this gap by itself: its UUID/display-name
association is not currently a secret-backed claimant verifier. A claimant's
ability to quote historical messages or filesystem paths is likewise not a
cryptographic or administratively delegated authority. First-come name-only
claim would convert a known D7 weakness into permanent account takeover.

Therefore automatic authority establishment is impossible from current
null-row data alone. A flow under the claim umbrella must rely on authority
external to the row that was either established beforehand or explicitly
delegated by a trusted administrator at the time of the operation.

### Candidate proof authorities

| Option | What the proof actually establishes | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| Challenge a pre-existing server-recorded cryptographic/runtime binding over its private channel | The claimant controls a binding created before the claim and already associated with this exact project/name | Null rows without such a binding cannot self-serve; loss of the private channel needs recovery | Only verifiably pre-existing bridge/binding records qualify; a binding created by the claimant during claim proves nothing |
| Project administrator approves through a separately authenticated local control plane, optionally strengthened by OS peer credentials | A designated administrator delegates ownership; it does not prove the original process returned | Requires a defined project-admin authority, human UX, headless policy, and audit; shared OS accounts weaken peer-credential meaning | Can recover any reviewed legacy row without pretending the row supplied proof; no current project-owner record is assumed |
| A project-admin credential authorizes the claim remotely | Holder of an established project-level authority delegates ownership | Introduces a high-impact credential and project ownership lifecycle; compromise affects many identities | Existing projects need deliberate admin enrollment before this can help |
| A quorum of already authenticated project peers witnesses the claim | A selected peer set agrees on continuity | Peers may be unavailable or collude; quorum policy and conflicts are complex | Projects with too few authenticated peers cannot use it; historical messages alone do not count as a witness |
| A pre-issued recovery code or hardware-bound recovery key proves continuity | Claimant possesses recovery material established while authority was healthy | Cannot help rows for which no recovery material was issued; custody and loss become user responsibilities | Applies prospectively, not to today’s unauthenticated null-token rows |
| Create a new enrolled identity and perform an administrator-approved continuity transfer/alias | No unsupported ownership claim is made; history continuity is an explicit administrative action | Stable name, inbox, reservation, and audit semantics may change; transfer tooling is substantial | Preserves the old row as evidence and can link it to a new principal without silently rewriting history |
| First caller knowing project and name claims the row | Only knowledge of public/ambient identifiers | Permits takeover and races; all null-token owners are affected | Converts every null row to attacker-wins-on-first-use and is not a meaningful authority proof |

The product must decide which external authority exists before endpoints or
schemas are designed. “Local machine” is not precise enough: a private socket
with verified peer credentials, a user confirmation, and an already-bound
bridge have different trust scopes.

### Authority-establishment invariants independent of the mechanism

Any viable authority-establishment flow should satisfy all of these properties:

- Bind the proof to the exact immutable project ID, agent row ID, canonical
  name, credential version, claimant, nonce, and expiry.
- Re-read and compare-and-swap the row in one transaction so exactly one
  concurrent claimant can move `unclaimed -> enrolled`.
- Refuse silent replacement of a non-null credential; that is rotation or
  recovery, not claim.
- Treat a retired row, a row with active reservations, and a row with unread
  work as explicit cases rather than side effects of enrollment.
- Persist no new owner credential until both the authority proof and the
  selected secret-delivery acknowledgement succeed, or define a retryable
  pending state that exposes no owner operations.
- Make wrong, expired, replayed, cancelled, and losing-race proofs produce zero
  changes to DB metadata, profile bytes, archive Git state, reservations,
  inbox read state, and signals. This is a prospective claim invariant with the
  same failure-atomic shape as D1, not an expansion of D1's selected and
  implemented scope.
- Audit authority type, approver/binding identifier, old/new enrollment state,
  timestamp, and a non-secret credential fingerprint. Never audit the secret.
- Make retries idempotent by receipt without returning the reusable credential.

The state labels (`unclaimed`, `pending`, `enrolled`, and any legacy variants)
are illustrative; this document does not select a schema.

## Rotation, loss, compromise, and recovery

These are different operations and must not share a weak fallback:

- Rotation: the caller still has the current credential.
- Loss recovery: the caller no longer has the current credential.
- Compromise recovery: the old credential may be controlled by an attacker.
- Null-token claim: the row never had a usable owner proof.

### Options and breakage

| Option | Suitable case and guarantee | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| Current-credential-authenticated atomic rotation | Normal rotation; old credential authorizes replacement, with an optional short explicit grace window | Clients that cannot update their private store atomically can lose continuity; grace permits temporary dual authority | Works for owners that possess existing tokens; unavailable generated tokens cannot use it |
| Pre-issued single-use recovery codes/keys stored as verifiers | Loss or compromise when recovery material was prepared earlier | Users must store recovery material; code theft is takeover; codes need revocation, replenishment, and replay protection | Prospective only unless codes are securely issued after existing ownership is proven |
| Recovery through a later-selected project-admin/local authority | Reviewed recovery without the old credential | Requires availability and trust of the administrator; unattended jobs may stop | Can cover generated-unavailable and null cohorts after an explicit review; should not rewrite history silently |
| Recovery through a pre-existing non-exportable bridge binding | Proves control of a bound runtime even if its bearer file is lost | Binding migration or bridge loss leaves no route; must resist a newly fabricated binding | Only rows with a qualifying prior binding benefit |
| New identity plus audited continuity transfer | Safest fallback when no valid authority survives | Stable name and history consumers may need alias/transfer support | Does not destroy the old row; existing mail and audit can remain attributable |
| Server reveals the stored plaintext credential | Restores access if plaintext exists | Database/server operators become credential delivery authorities; disclosure cannot distinguish owner from requester and defeats one-way storage | Impossible after verifier migration; exposes current plaintext rows and backups |

An old-token rotation should be a compare-and-swap: verify the old credential,
install the new verifier/sink receipt, increment credential version, then revoke
the old version. The decision must specify whether a grace window exists, who
can end it early after suspected compromise, and how a crash between sink and
DB commits is reconciled. Loss recovery must never fall back to the ambient
facts rejected in the null-claim section, and a plaintext credential is never
“recovered”; ownership is re-established and a new credential is rotated in.

## `macro_start_session`

The current macro combines identity creation/reuse, optional reservation, and
inbox access without a credential parameter. Under a strict enrollment design,
the macro must not perform reservation or inbox work until identity ownership
has been established. Merely adding a plaintext token parameter would also put
the secret on the model-facing tool and Rich-log paths.

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Trusted launcher/proxy enrolls first, then calls the macro through a session-bound identity with credential injection hidden from the model | Direct callers must bootstrap before one-call convenience; proxy outages fail closed | Existing valid private token files/bindings can continue; null or unavailable-token rows need claim/recovery |
| Macro accepts a non-secret credential reference and resolves it only in a trusted server/proxy boundary | Requires reference lifecycle, authorization, and remote semantics; arbitrary file paths must be rejected | Existing stores need a reference adapter; rows themselves are unchanged |
| Macro performs a two-phase enrollment and returns `enrollment_required`/pending receipt until a secret sink acknowledges | No immediate reservations or inbox on first call; clients need retry/resume logic | Prevents new null rows; existing null rows remain a claim problem |
| Macro accepts a literal owner token and delegates to authenticated registration | Simple but exposes the credential to transcripts and instrumentation unless the macro is never model-facing and logging is fixed | Known-token callers work; generated-unavailable and null rows still fail |
| Keep creating legacy-unclaimed rows, then establish authority later | Rejected for the selected D7 rollout: it continues name-only reservation/inbox/retirement exposure and creates more migration debt | Existing and new null rows remain mixed, so enforcement cannot begin |
| Macro returns a generated plaintext token | Preserves one-call enrollment but recreates the documented result/archive leak and makes the broad macro result a secret container | Existing null rows still require claim; newly returned secrets require rotation if logged |

Whichever option is selected, macro reuse of an existing name must authenticate
before metadata refresh, and rejected authentication must be side-effect free.
The same rule applies to internal welcome messages, reservation ownership, and
inbox read/ack state. The inventory must cover both other macros that directly
call `_get_or_create_agent` and the D3-confirmed non-macro cross-project alias
path; `macro_start_session` is not the only source of new null-token rows.

## Zero-lockout, rollback-capable migration order

“Zero lockout” means that no existing cohort loses its current compatible path
before it has a verified replacement or an explicitly reviewed legacy window.
It does not mean a suspected-compromised credential remains enabled, nor does
it promise perpetual name-only access.

### Cohorts that must remain distinguishable

| Cohort | Present evidence | Migration risk / existing-data treatment |
|---|---|---|
| Non-null token with a matching caller/private-store copy | Current credential can prove continuity | Lowest risk; verify by fingerprint/challenge without exporting the DB value |
| Non-null token whose origin or caller copy is unavailable | Server has a bearer, apparent owner may not | Treat as recovery, not null claim; do not expose or silently replace the stored value |
| Null-token macro/legacy row with no qualifying external binding | No owner proof | Grandfather temporarily or require administrator-approved claim/new identity; automatic claim is unsafe |
| Null-token target-project alias created by cross-project send | D3 proves the alias and its source metadata, but not owner authority or safe deduplication | Keep distinct from macro/legacy rows; migration must preserve origin/project attribution or quarantine ambiguity |
| Null-token row with a qualifying binding recorded before migration | External continuity evidence may exist | Validate binding provenance and exact project/name association before allowing claim |
| Retired, deregistered, or operationally encumbered row | Lifecycle/work state in addition to credential state | Decide whether claim restores activity, preserves retirement, or requires separate lifecycle action |

The server cannot infer the first two cohorts solely from a non-null DB value.
Client-side possession must be proven without comparing/exporting raw values in
an inventory artifact.

### Ordered rollout

1. **Freeze semantics and inventory code paths, not live data.** Enumerate every
   tool, macro, launcher, bridge, dashboard route, and internal call that
   creates, reads, verifies, transports, or conditionally bypasses credentials.
   Add secret redaction before collecting any telemetry.
2. **Add only additive state and observability.** Introduce versioned
   enrollment/legacy markers, receipts, and non-secret fingerprints behind a
   feature flag. Do not reinterpret null or non-null values yet. Keep old
   readers able to ignore new fields.
3. **Upgrade credential holders first.** Teach launchers and proxies the chosen
   private transport, atomic store update, and rollback-compatible dual read.
   Direct clients receive explicit compatibility diagnostics. No strict server
   denial is enabled.
4. **Stop creating new null-token identities, then use the new path for new
   identities.** Do not create a usable owner identity until secret delivery is
   acknowledged. This prerequisite is selected but not implemented here. A
   rollback may pause new enrollment, but it must not resume null-token
   creation; already enrolled identities keep working rather than having their
   credential erased.
5. **Run an authorized claim/recovery campaign.** Classify only by proof, not
   by name or activity. Null rows without proof stay visibly legacy-unclaimed;
   unavailable-token rows use recovery. Do not bulk-write synthetic owner
   tokens and call that enrollment.
6. **Shadow every proposed strict-D6/D7 denial.** The first transport-independent
   observer records exactly principal candidate, tool, `would_allow` or
   `would_deny`, and reason while preserving current behavior. It records no
   credential value. Extra cohort or client fields require a later
   privacy-reviewed schema revision. Measure macro/internal callers as well as
   published tools before enforcement.
7. **Enable strict behavior per opted-in identity/project.** Require an
   explicit readiness check and retain an operator kill switch. Rollback
   changes enforcement mode, not credential history; it must not turn an
   enrolled credential back into null.
8. **Change the default only after reviewed coverage thresholds.** Unselected
   legacy rows remain in an explicit compatibility cohort with a published
   deadline or administrator action. Replacing D6's selected Path-A parity with
   strict behavior needs a new decision; D7 still needs separate
   implementation/cutover approval. Neither change is authorized here.
9. **Remove legacy credential representation last.** One-way verifier cleanup,
   old client removal, and deletion of plaintext/token duplicates happen only
   after restore drills, backup review, and expiration of the agreed rollback
   window.

### Rollback requirements

- Schema changes remain additive while rollback is supported; old binaries
  must tolerate new rows/fields or be barred by an explicit version gate.
- A server flag can restore legacy enforcement for grandfathered cohorts
  without deleting newly enrolled credentials or claim audit.
- Client stores support the previous and new credential versions until the
  rollback window closes. Rotation commits record which version is active.
- Failed rollout does not restore a row to null automatically: that would
  discard newly established authority and reopen D7. Rollback changes policy,
  not ownership facts.
- Rollback never re-enables creation of new null-token identities. If the new
  enrollment path is unavailable, new creation pauses or fails closed while
  existing compatible identities retain their separately selected behavior.
- No migration deletes a plaintext value, private client copy, binding, or
  recovery verifier until a tested recovery path and backup rollback exist.
- Shadow and enforcement logs contain fingerprints/reasons only and can be
  disabled without losing the underlying identity state.

## Option impact summary

The cross-cutting breakages that a product decision must explicitly accept are:

| Selected capability | Principal breakage | Existing data that needs handling |
|---|---|---|
| Any strict owner authentication | Tokenless and unavailable-token callers stop mutating/sending once their compatibility window ends | Null rows, generated-but-unavailable tokens, internal macro identities |
| Any transport that keeps secrets outside model tools | Direct one-call clients need launcher/proxy/bootstrap support | Existing private token files and bridge bindings need provenance/permission validation |
| Any one-way server verifier | Plaintext retrieval and exact old-binary writes no longer work after cleanup | Plaintext indexed token column, backups, fixtures, and dual-read period |
| Any administrator-backed claim/recovery | Someone or something becomes a higher authority than an agent row | Project-admin enrollment, audit, revocation, shared-machine and headless policy |
| Any non-exportable binding | Identity mobility and disaster recovery depend on the bridge | Binding provenance, private-channel availability, transfer across processes/machines |
| Any continuity transfer instead of claim | Stable-name consumers and lifecycle semantics must understand aliases/transfers | Inbox, thread, reservation, audit, retirement, and historical attribution |

This table is a checklist, not a ranking.

## Non-binding lean for evaluation

This lean exists only to focus prototypes and tests. It does not select the
enrollment or authority mechanism, change D6 or D1's selected and implemented
scope, weaken the
ledger-selected D7 boundary, or authorize D6/D7 enforcement.

- Prefer caller-generated high-entropy credentials held by a trusted
  launcher/proxy, or server-generated credentials delivered to an authenticated
  one-time sink. Return only a receipt/fingerprint to model-facing tools.
- Prefer one-way versioned server verification after a dual-read rollback
  period; never design normal recovery around revealing a stored bearer.
- For a null row, evaluate a pre-existing private binding challenge when one
  genuinely predates the authority-establishment request; otherwise evaluate
  explicit project-admin/local approval. If neither exists, create a new
  enrolled identity and use an audited continuity transfer rather than
  first-come name claim. These remain mechanism candidates, not selections.
- Use current-credential-authenticated atomic rotation for normal rotation;
  use pre-issued recovery material or the later-selected administrator/binding
  authority for loss. Do not reuse ambient identity facts.
- Have a trusted bootstrap enroll/bind before `macro_start_session`; keep
  reservation and inbox operations behind the authenticated boundary.
- Migrate additively through client readiness, opt-in enrollment, claim/
  recovery, shadow denials, per-identity strict mode, and only then a separately
  approved default change.

The lean remains contingent on defining project-admin authority, remote secret
sinks, verifier key custody, and continuity-transfer semantics.

## Post-decision tests

After the relevant mechanism and implementation are approved, these tests
become committed, hermetic gates before an entry's `implementation_state` can
change. D1's existing selected-requirement test remains a prerequisite rather
than an open choice.

### Enrollment and non-disclosure

- New identity with the chosen caller-generated, sink, and/or bound transport;
  assert canonical read-back, credential version, receipt, and exactly one
  active verifier.
- Crash before DB commit, after DB commit but before sink acknowledgement, and
  after sink acknowledgement but before response; retry must be deterministic
  and must neither leak nor mint an unknown extra owner credential.
- Wrong sink, expired handle, replayed handle, reused receipt, and concurrent
  enrollments; all rejected paths have zero durable/profile/Git side effects.
- Canary-scan request capture, every result format, Rich logs, exceptions,
  telemetry, JSONL, profiles, messages, archive/Git, dashboard, notifications,
  process listings, environment dumps, shell history, temporary files, client
  state, and diagnostics. Allow an occurrence only in the intentionally chosen
  verifier/secret store.
- Permission, symlink, non-regular-file, partial-write, fsync, atomic replace,
  cleanup, and backup/export tests for every supported secret sink.

### Claim authority and races

- For every accepted proof authority, test correct project/name/row binding,
  wrong identity, wrong project, expiry, cancellation, replay, forged/newly
  created binding, revoked administrator, and unavailable verifier.
- Attempt claims using only name, project key, `AGENT_NAME`, tmux/window ID,
  task metadata, historical message, filesystem path, and inbox knowledge;
  each must fail unless the chosen policy explicitly makes a separately
  authenticated control plane—not the fact itself—the authority.
- Run two different claimants and duplicate retries concurrently; exactly one
  transition and one credential version may win, with no losing metadata,
  archive, reservation, or inbox mutation.
- Cover active, retired, deregistered, unread-work, and active-reservation rows;
  assert the selected lifecycle behavior separately from ownership.

### Rotation and recovery

- Correct, missing, wrong, old, new, and grace-window credentials before,
  during, and after atomic rotation.
- Crash and retry at every verifier/sink boundary; assert at least one intended
  owner path remains usable without leaving unintended dual authority.
- Recovery-code single use, replenishment, revocation, theft/replay, and
  concurrent compromise recovery.
- Lost bridge/admin/recovery paths and the final new-identity continuity
  transfer, including attribution of historical mail and reservations.

### D1, D6, D7, and macros

- D1: snapshot the row, timestamps, profile bytes, archive HEAD/log, and Git
  status; an explicitly conflicting token must have zero rejected-call mutation,
  while a same-token update commits once. Pin omitted-token compatibility
  without treating it as D1 authority, and race distinct explicit tokens
  against a null-token row so only one DB writer and one profile commit win.
- D6: retain the selected omitted-token parity case, then separately evaluate
  any future strict behavior for caller-known token, generated-unavailable
  token, null legacy, enrolled, pending, and grandfathered cohorts; test correct/wrong/missing
  sender authority and assert `verified_sender`, DB recipients, archive, inbox,
  and zero rejected-call mutation.
- D7: split active-null and already-retired-null legacy cohorts. Prove that
  name-only re-retire succeeds only for the already-retired null-token cohort,
  preserves `retired_at`, and changes no DB/profile/archive/signal state. Prove
  that an active null-token row is not auto-retired and that unretire, hard
  delete, transfer, and project-wide owner operations
  require the future principal/admin authority. Also prove every new-identity
  path has stopped creating null-token rows before enforcement is enabled.
- `macro_start_session`: fresh enrolled identity, existing valid identity,
  existing conflicting identity, null legacy row, unavailable generated token,
  pending sink, and retry after crash. No reservation or inbox access may occur
  before the chosen authenticated/enrolled state.
- Inventory and test other internal macro/welcome paths that create or act as
  an identity so strict enforcement cannot strand a hidden caller.

### Migration and rollback

- Current and previous client versions against legacy, dual-read, shadow,
  opt-in strict, and default-strict server modes.
- Cohort-by-cohort migration with aggregate counts only; verify no raw token is
  emitted by inventory or shadow telemetry.
- Flip the kill switch during enrollment, claim, send, rotation, macro start,
  and service restart; newly established ownership remains intact while the
  explicitly retained legacy compatibility behavior returns. New null-token
  creation must remain disabled.
- Restore old binaries and backups at every declared rollback milestone; fail
  closed at an explicit version gate if an old binary cannot safely read the
  additive state or, after the stop-null milestone, can create a new null-token
  identity.
- Fresh-install coverage remains mandatory: a clean installation must enroll,
  restart, rotate, send, retire, and recover without depending on developer
  machine state.

## Unselected matters requiring a product decision

- What is the canonical project-administrator authority, how is it enrolled and
  revoked, and how does it work on shared machines and in headless CI?
- Which pre-existing bridge/window/session bindings are strong enough to prove
  continuity, and how is “predates the claim” made tamper-evident?
- Which secure sink works for remote MCP clients where a local path or file
  descriptor has no meaning?
- Is the server verifier plaintext, keyed one-way, or externally managed; who
  holds verifier keys; and how long is dual-read rollback retained?
- How are currently tokenized rows classified without exporting their tokens,
  especially server-generated values that were never returned?
- What are the custody, expiry, and replenishment rules for recovery material?
- Does continuity transfer preserve the canonical name, create an alias, move
  inbox/reservations, or only link audit history?
- What exact principal/admin credential, delegation, and audit mechanism would
  turn the prospective 24-tool permission inventory into selected policy?
- How should each authority-establishment mechanism handle active reservations,
  unread mail, signals, and concurrent metadata refresh without implicitly
  unretiring or transferring an identity?
- What audit retention is sufficient without turning fingerprints, OS identity,
  or approver metadata into a new privacy/security liability?
- What readiness threshold and compatibility deadline are required before
  replacing D6 parity with strict behavior or changing D7
  implementation/cutover?
