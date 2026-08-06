# AgentStack Mail product decision evidence

Status: evidence packet, not the normative decision record. Items may move from
pending to resolved as choices are implemented and verified. The authoritative
status, selected scope, and verification remain in
`packages/agentstack_mail/fixtures/differential-expected-divergences-v1.json`.
No entry in this document approves authority cutover.

## Observation method and limits

The frozen live source was authenticated and reconstructed only from the
checked-in Git bundle plus tracked patch by
`packages/agentstack_mail/tests/differential_source.py`. Frozen live and Core
were then run through FastMCP clients in separate worker-private databases,
archives, signal roots, homes, and project directories. Fault and race probes
used only those temporary roots. No installed AgentMail service, existing
database, MCP key, development endpoint, ORRERY process, or authority setting
was read or changed.

The committed Behavior scenarios remain the reproducible success-path base in
`packages/agentstack_mail/tests/test_differential.py`. Edge probes used for this
packet were one-off diagnostics; each section therefore gives the operation,
fault/race, durable assertion, and relevant source seam needed to reproduce it.
After a product choice, that recipe must become a committed test before the
manifest entry can change. The prevalence of these states in production data is
unknown because inspecting the running database was intentionally out of scope.

All dynamically proven cases originally matched between frozen live and Core.
A selected resolution may intentionally make Core diverge; matching an unsafe
or ambiguous live behavior is evidence, not acceptance.

## One-line summary

| ID | Observed or selected behavior | Decision tension | Principal impact |
|---|---|---|---|
| D1 | Frozen live mutates before rejecting a conflicting token; selected Core behavior rejects before durable mutation | restart compatibility versus owner authentication and failure atomicity | identity owners, dashboard/profile readers, divergent DB/archive state |
| D2 | Expired pending links can be accepted and expired approved links still authorize delivery | continuity versus TTL as a real authorization boundary | contact-controlled and cross-project senders/recipients; stale grants |
| D3 | Cross-project intros use a foreign sender row; reply fails, while later sends create a target-local alias and replies stay there | project-local schema versus authentic routable origin identity | replies, audit, same-name agents, existing aliases/messages |
| D4 | Accepting without a pending request creates an approved link | out-of-order convenience versus consent/audit provenance | contact owners and any caller who knows both names |
| D5 | An invalid contact policy silently becomes `auto` | forgiving clients versus fail-fast policy configuration | operators, policy audits, indistinguishable historical `auto` values |
| D6 | A tokenized sender may omit `sender_token` and send unverified | legacy clients versus sender authentication | all recipients/auditors; identities whose generated token is unavailable |
| D7 | Tokenless macro/legacy identities can be retired by name alone | macro operability versus owner authorization | tokenless identities and owner-operation callers |
| D8 | A failed archive write leaves committed DB agent/message state | live ordering versus cross-store consistency and truthful failure | senders, recipients, dashboard, Git/archive consumers |
| D9 | Ack failure after the first helper leaves `read_ts` committed and `ack_ts` null | independently durable read progress versus atomic acknowledgement | receipt readers, retry logic, legacy partial rows |
| D10 | One shared archive lock yields one scheduler-dependent winner; one DB with split archive roots can store conflicting winners | local simplicity versus topology-independent correctness/fairness | parallel agents, HA/misconfigured deployments, existing duplicate leases |
| D11 | Retirement preserves active reservations, unread work, and signals; retired agents can still fetch | reversible soft retirement versus immediate handoff and explicit work disposition | peers blocked by leases, senders awaiting ack, operators |
| D12 | Message state can commit before a crash loses its signal; filtered fetch clears every signal | send availability versus durable wakeups, retry, and per-message acknowledgement | offline/stale consumers, watchers, notification operators |

## D1 — conflicting token registration mutation

### 1. Observed live behavior

Register `GreenCastle` with token A, then re-register the same name with token B
while changing every mutable field. The call raises a token-mismatch error, but
the database has already changed `program`, `model`, `task_description`,
`attachments_policy`, and `last_active_ts`. Token A remains. `profile.json` is
rewritten and a Git commit is added; the profile contains the new
program/model/task but the old attachment policy, so DB and archive diverge.

Reproduction seam: frozen `app.py` updates the existing row in
`_get_or_create_agent`, then `register_agent` validates the requested token.
Core now authenticates and persists registration state inside
`_get_or_create_agent`; `register_agent` selects that managed-token path.

### 2. Current Core behavior

Core now validates an explicitly supplied token against an existing identity
before changing metadata, touching a window identity, or writing the profile.
A conflict returns the chosen authentication error with byte-identical
DB/profile/archive/Git state and no commit; a same-token metadata refresh
creates exactly one Git commit. For a legacy null-token row, a conditional
SQLite update makes one concurrent registration the atomic writer; a different
token arriving after that write is a conflict and cannot add metadata or a Git
commit. The current first-DB-writer outcome is retained unsafe compatibility
arbitration, not accepted ownership proof. This serialization does not decide
who is entitled to claim the row: the D6/D7 authority behavior remains pending,
and its eventual decision may replace this arbitration and its regression test.
The committed requirement tests are in
`packages/agentstack_mail/tests/test_pending_decision_d1.py`; the manifest
remains authoritative for selected scope and status.

### 3. Why it was pending

Live-compatible re-registration permits restart metadata refresh, but owner
authentication and the normal meaning of a rejected call require zero durable
mutation. Authenticating every existing-name registration also collides with
the tokenless recovery cases in D6 and D7.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve live ordering | Failed authentication can still deface identity metadata and create DB/archive divergence; owners and observers lose a trustworthy failure boundary | None |
| Prevalidate only an explicitly conflicting token before any mutation | Only clients relying on rejected calls to mutate state break; omitted-token semantics remain as today | None; historical divergence still needs audit if desired |
| Require valid owner credentials for every existing-name re-registration; use a separate rotate/recovery operation | Tokenless legacy launchers and identities with unavailable generated tokens cannot refresh until claimed | Rows without usable credentials need claim/backfill/recovery |

The selected narrow closure prevalidates an explicit conflict. The broader
omitted-token policy remains separate with D6 and D7; this prose does not replace
the manifest's normative resolution record.

### 5. Test that fixes the choice

The committed selected-requirement tests snapshot SQLite bytes, profile bytes,
the complete archive worktree, and Git internals after token A registration.
Token B with changed program/model/task/attachment metadata must return the
selected auth error with byte-for-byte unchanged durable state and no commit;
the existing-window route is exercised separately. A same-token update refreshes
all metadata and commits exactly once. The omitted-token case preserves the
legacy credential/authority semantics, one profile commit, and the existing
profile-before-DB attachment ordering; it serves only as a compatibility
regression for pending D6/D7. A deterministic
two-client rendezvous against a legacy null-token row proves exactly one atomic
winner, one selected auth error, one Git commit, winner-only metadata, and no
credential disclosure in either result under the retained compatibility path.
It does not prove that the winner is an authorized claimant and must be revised
with the D6/D7 test matrix when claim authority is selected.

The result/profile assertions above are not an end-to-end secret logging gate.
The current Rich request instrumentation can still receive raw credential
arguments; redaction or proxy injection remains an explicit, unimplemented
requirement in the claim/enrollment design.

## D2 — expired contact link accepted

### 1. Observed live behavior

Set a pending link expiry to the year 2000, then call `respond_contact` with
`accept=true`: it becomes approved and receives a fresh expiry. Set an approved
link expiry to the year 2000: it still authorizes delivery to a
`contacts_only` recipient. The frozen send and response queries filter status
but not expiry around `app.py:6304` and `:7424`; Core has the same omission
around `app.py:6333` and `:7456`.

### 2. Current Core behavior

Identical. Expiry is stored and returned but is not an authorization boundary
in the proven response/send paths.

### 3. Why it is pending

Treating TTL as revocation conflicts with live continuity for stale links.
Immediate enforcement may disable an unknown number of existing expired
pending or approved rows.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Keep expiry advisory | TTL remains misleading; expired grants authorize local and cross-project traffic | None |
| Enforce `expires_ts > now` in response, send, reply, and external routing | Stale workflows stop immediately; contact-controlled clients must renew | Expired rows need reporting, renewal, or explicit invalidation |
| Stage a legacy cutoff/grace period while enforcing newly created rows | Semantics differ temporarily by row age; clients and operators need visibility | Requires a cutoff/status migration and audit list |
| Auto-refresh or re-handshake on use | Revocation is weakened because attempted access silently renews permission | Existing links continue but access attempts mutate them |

Non-binding lean: make expiry real, but first inventory and expose stale rows;
do not hide enforcement behind automatic renewal.

### 5. Test that fixes the choice

Use an injected clock or private DB fixtures for expired pending, expired
approved, unexpired, and null-expiry links. Cover response, local send,
cross-project route, and reply with auto-handshake disabled. Strict denial must
produce the chosen structured error and zero message/archive writes. A staged
choice also needs both sides of the cutoff and idempotent migration tests.

## D3 — cross-project intro/reply identity

### 1. Observed live behavior

A project-A contact intro stored in project B points at Green's project-A agent
row. Replying from B fails because reply resolves that sender under project B:
`Agent id '1' not found for project B`. After approval, a normal A-to-B send
creates an unauthenticated `GreenCastle` alias in B with a null token. Blue's
reply then succeeds but reaches that B-local alias; Green's project-A inbox
stays empty.

The schema has independent message-project and sender foreign keys without a
same-project invariant (`models.py` around the `Message` declaration). Frozen
cross-project send creates a target-local alias around `app.py:6851`; reply
constrains lookup to the current project around `:7046`. Core has the same
model and paths around `app.py:6883` and `:7078`.

### 2. Current Core behavior

Identical failure, alias creation, and misrouted reply.

### 3. Why it is pending

Authentic, routable cross-project identity conflicts with the current
project-local message/archive assumptions and name-based routing. A same-name
agent in multiple projects makes inference unsafe.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve foreign intro senders and local aliases | Replies remain broken or misleading; audit and ownership are not trustworthy | None; ambiguity remains |
| Add first-class origin project/agent identity or a global principal binding and route replies by ID | Schema, payload, archive, dashboard, and compatibility change | Foreign-sender messages and null-token aliases need deterministic migration/deduplication |
| Keep target-local proxies but attach immutable origin IDs and bridge replies to origin | Proxy lifecycle and token/name confusion remain; bridge logic becomes mandatory | Existing aliases require origin backfill; unmappable rows need quarantine |
| Reject cross-project messaging for the first authority cutover | Existing cross-project callers and links stop working | Preserve old records read-only or quarantine; create no new ambiguous rows |

Non-binding lean: do not authorize cross-project writes until either a
first-class origin model is fixed or the feature is explicitly disabled.

### 5. Test that fixes the choice

Create two projects, then repeat with the same agent name in both. Exercise
request intro, approval, send, reply, and process restart. Assert the selected
message sender/project invariant, immutable origin provenance, thread
continuity, and delivery to the source inbox. Reject mode must fail before any
link, message, alias, or archive commit. A migration fixture must contain both
a foreign-sender intro and null-token target alias and yield a deterministic
mapping or explicit quarantine.

## D4 — accept response without pending

### 1. Observed live behavior

With two fresh agents and no link, `respond_contact(accept=true)` returns
`approved=true`, `updated=1`, and creates an approved link with empty reason and
future expiry. The frozen creation branch is around `app.py:7440`; Core matches
around `app.py:7472`.

### 2. Current Core behavior

Identical. Acceptance is also an approval-creation operation, not solely a
response to a stored request.

### 3. Why it is pending

Out-of-order convenience conflicts with the consent/audit invariant that a
response corresponds to a real, unexpired pending request. Administrative
contact establishment is a separate authority question.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve auto-create | Any caller knowing both names can synthesize approval; there is no request provenance | None |
| Require an unexpired pending request | Out-of-order clients and macros fail with `NO_PENDING` | Existing approved rows may remain unchanged |
| Make response strict and add an authenticated `establish_contact` operation | Adds a privileged surface and client migration | Existing links remain; future admin links gain provenance |
| Permit no-pending accept only with target-owner credential | Tokenless callers break and the rule depends on unresolved owner auth | Existing links remain |

Non-binding lean: require pending state for ordinary response; if administrative
establishment is required, make it a separately authorized and audited action.

### 5. Test that fixes the choice

With no link, test accept and reject while snapshotting link/message/profile/Git
state. Strict mode must return `NO_PENDING` with no writes. Then test unexpired
pending acceptance, replay after approval, and expired pending under D2. Any
privileged path must reject ordinary/wrong-token calls without mutation and
create exactly one audited approval with valid authority.

## D5 — invalid contact policy coerced auto

### 1. Observed live behavior

Calling `set_contact_policy(..., policy="not-a-policy")` succeeds and returns
`{"agent":"BlueLake","policy":"auto"}`; the DB stores `auto`. Frozen source
lowercases and coerces unknown values around `app.py:7512–7521`. A nonempty
invalid string was dynamically proven. Empty input also maps to `auto` by
source inspection but was not dynamically probed.

### 2. Current Core behavior

Identical result and AST-equivalent body at
`packages/agentstack_mail/src/agentstack_mail/app.py:7544`.

### 3. Why it is pending

Forgiving input compatibility conflicts with fail-fast policy configuration. A
typo can silently change the intended access posture.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve coercion | Typo detection and policy auditing remain unreliable | Intentional and accidental historical `auto` are indistinguishable |
| Reject unknown/empty values without mutation | Loose clients using invalid or empty values as defaults fail | No migration; past accidental values cannot be recovered automatically |
| Compatibility mode warns/coerces while strict mode rejects, then schedule a default change | Dual semantics and warning/audit plumbing add complexity | Future coercions can be audited; past ones remain unknown |

Non-binding lean: reject unknown values and accept only documented values or an
explicit default syntax.

### 5. Test that fixes the choice

Start at `contacts_only`; test every valid value, mixed case, one invalid value,
and empty input. Assert exact response/error, persisted policy, and unchanged DB
on rejection. Add a downstream delivery check. A transitional choice must
parameterize modes and assert its warning/audit event.

## D6 — missing sender token succeeds

### 1. Observed live behavior

A tokenized `GreenCastle` can omit `sender_token`: send succeeds with
`verified_sender=false`, and DB/archive delivery commits. A wrong supplied
token fails. Frozen source verifies only inside `if sender_token is not None`
around `app.py:6192–6199`.

### 2. Current Core behavior

Identical in the `send_message` conditional sender-token verification. The
proven DB contains only the successful omitted-token message when a wrong-token
control is also attempted.

### 3. Why it is pending

Legacy clients omit credentials, but any caller knowing an agent name can then
impersonate it. Strict enforcement is complicated because registration without
a supplied token generates a token that the response intentionally does not
return; some apparent owners cannot possess it.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve optional verification | Sender ownership remains unenforced for omitted tokens; recipients and audits cannot trust `from` | None |
| Require the matching token whenever the row has one | Old clients, generated-token owners, and tokenless internal welcome paths may stop sending | Some identities require re-enrollment before they can send |
| Versioned enrollment: grandfather legacy identities, require caller-owned tokens for new ones, and add claim/rotation | Schema/enrollment/migration complexity and temporary mixed semantics | Existing rows can be marked legacy instead of locked out |

Non-binding lean: do not flip strict enforcement until provenance, claim/
rotation, and internal macro credential propagation are designed.

### 5. Test that fixes the choice

For known-token, server-generated-token, macro-created null-token, and migrated
legacy identities, try correct, wrong, and missing credentials. Assert result,
`verified_sender`, DB/recipient counts, archive, inbox, and zero rejected-call
mutation. Add registration-then-send, macro welcome, any rotation path, and
credential non-disclosure scans.

## D7 — owner tools name-only auth

### 1. Observed live behavior

`macro_start_session(agent_name="RedStone")` creates a row whose token is null.
`retire_agent` then succeeds with only project and name and persists
`retired_at`. Omitting the token for a tokenized control agent fails. Frozen
macro creation and conditional retirement guards are around `app.py:7864` and
`:5041`. Broader unpublished owner tools have analogous source guards but were
not all dynamically probed.

### 2. Current Core behavior

Identical for the published `retire_agent` path and the macro's direct
`_get_or_create_agent` call. Broader owner-tool behavior remains unproven
dynamically.

### 3. Why it is pending

Tokenless macro/legacy identities must remain operable, while a name alone is
not proof of ownership. `macro_start_session` currently cannot accept or return
a caller-owned token.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve conditional name-only authorization | Any caller knowing a null-token identity name can retire it | None |
| Require credentials for every owner operation | Tokenless macro/legacy identities cannot retire, restore, deregister, or delete themselves | Null-token rows require claim/enrollment |
| Tier by operation: allow name-only soft retirement but require token/admin for restore or irreversible actions; add claim | Name-only denial of service remains; authorization becomes operation-specific | Null-token rows remain usable but need an `unclaimed` interpretation |

Non-binding lean: add an explicit claim path, then fail closed for owner
mutation; do not solve this by leaking generated credentials from macros.

### 5. Test that fixes the choice

Build a matrix for caller-supplied-token registration, generated-token
registration, macro-created null token, and migrated null token. For each owner
operation retained in the product, test missing/wrong/correct authority and
exact DB state. At minimum cover published retirement, macro reuse of a
tokenized identity, and result/log credential non-disclosure.

## D8 — DB persists after archive failure

### 1. Observed live behavior

Injecting `write_agent_profile` failure during new registration returns an
error but leaves the DB agent row, with a null token and no profile. Injecting
`write_message_bundle` failure during send returns an error but leaves message
and recipient rows, with no corresponding archive message. Frozen source
commits agent/message state before profile/bundle writes around
`app.py:3191–3239` and `:3446–3484`/`:4676–4683`.

A committed literal-SIGKILL probe now kills the worker after the canonical,
outbox, and inbox files are written and staged but immediately before
`IndexFile.commit`. The DB message and recipient plus all three staged files
survive, Git HEAD does not advance, no message commit exists, and no signal is
emitted. This is pinned for frozen live and Core in
`packages/agentstack_mail/tests/test_pending_decision_d8_d9.py`.

The same committed probe wraps the existing `_write_text` seam and kills after
the first or second successful bundle write. The first case leaves only the
canonical file; the second leaves canonical plus outbox, with no inbox copy.
Both retain the DB message/recipient, leave Git HEAD unchanged with no staging
or message commit, and emit no signal.

Still unknown: instruction-level death inside Git's native commit, where no
direct Python seam exists. Other archive-writing tools were not dynamically
proven.

### 2. Current Core behavior

Identical injected results, complete and partial-bundle SIGKILL results, and
ordering around Core `app.py:3214–3268` and
`:3469–3507`/`:4699–4707`.

### 3. Why it is pending

SQLite and Git/filesystem cannot share one native transaction. Live DB-first
ordering conflicts with caller-visible atomicity, retry safety, and agreement
between dashboard/DB and archive consumers.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve DB-first partial commits | Error can mean delivered; retry may duplicate; DB and Git readers disagree | Existing DB-only rows remain and should be audited |
| Compensate DB on archive exception | Compensation can fail and cannot close a SIGKILL window; concurrent readers see transient state | Historical partial rows need separate reconciliation |
| Persisted saga/outbox with pending→archive→committed and idempotent recovery | Schema/API/readers/recovery become more complex | Backfill rows as committed, then reconcile missing archives |
| Declare DB authoritative and archive best-effort/rebuildable; expose degraded status | Current canonical-Git completion promise changes | DB-only rows can rebuild; archive-only artifacts still need audit |

Archive-first alone only trades DB orphans for archive orphans and does not
solve message IDs assigned by SQLite.

Non-binding lean: a persisted saga/outbox is the only listed option that also
covers crash recovery and idempotent retry.

### 5. Test that fixes the choice

Fault at pre-write, first filesystem write, pre/post Git commit, and process
termination in every persisted phase for registration and messaging. Assert
tool result, DB, archive/Git integrity, inbox visibility, signals, retry, and
duplicate count. The chosen option must pin either exact partial state,
compensated pre-state, invisible/recoverable pending state, or explicit
degraded DB success.

## D9 — read/ack partial commit

### 1. Observed live behavior

`acknowledge_message` calls the read and ack timestamp helpers sequentially;
each helper opens and commits its own session. Inject failure before the second
helper: the tool errors, while SQLite durably retains `read_ts != NULL` and
`ack_ts = NULL`. A committed worker probe now uses literal SIGKILL after the
read transaction commits but before the ack helper is called; reopening SQLite
retains `read_ts != NULL, ack_ts = NULL`. Frozen seams are around
`app.py:4416–4444` and `:7817–7818`, and the probe is in
`packages/agentstack_mail/tests/test_pending_decision_d8_d9.py`.

### 2. Current Core behavior

Identical at Core `app.py:4439–4467` and `:7849–7850`. The committed probe pins
this failure seam in addition to existing Behavior coverage of success and
replay.

### 3. Why it is pending

An independently durable read event can be useful progress, but callers may
reason that a failed acknowledgement changed neither field. Richer retry/audit
state would require a new model.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve partial commit | Failed ack can appear read; monitoring and callers need partial-success semantics | No migration; retry fills ack |
| Set both fields in one transaction | Live failure parity and independently durable read progress change | Existing read-only rows cannot be auto-acked safely |
| Add acknowledgement operation state/idempotency key (`pending/completed/failed`) | Schema/API and receipt readers become more complex | Partial rows must be classified legacy/unknown, not fabricated as acked |

Non-binding lean: use one transaction unless independently durable read
progress is a stated product requirement.

### 5. Test that fixes the choice

Fault before the first write, after the first write, before commit, and after
commit; then SIGKILL after the first durable phase and reopen/retry. Race two
ack calls and require stable timestamp/idempotency behavior. Include a legacy
`read!=NULL, ack=NULL` fixture and prove migration does not invent an ack.

## D10 — concurrent reservation winner and SQLite lock semantics

### 1. Observed live behavior

With one shared DB and archive root, both same-process and two-process barrier
races persist exactly one reservation. The named winner is scheduler-dependent:
Green won one same-process run; Blue won a process race launched Green first.
The archive lock encloses conflict read, creation, and archive write around
frozen `app.py:9084–9205` and `storage.py:645–780`/`:974–997`.

With one DB but different archive roots, each process takes a different lock;
both pass their conflict read and SQLite serializes two inserts, leaving two
active logically conflicting rows.

A committed probe records the production `PRAGMA busy_timeout` as 60,000 ms,
then uses the same checkout/commit path with a test-local 75 ms timeout and an
external `BEGIN IMMEDIATE` writer. The public call returns the sanitized generic
DB `ToolError`; no reservation row or archive projection is written. After the
writer rolls back, retry grants exactly one reservation. Four bounded
shared-root races per implementation each produced one grant and one conflict,
without naming a winner. The probe is
`packages/agentstack_mail/tests/test_pending_decision_d10.py`.

Exact wall time with the unscaled 60-second setting remains unmeasured because
multiple connection/check-in waits may accumulate. Finite black-box races
cannot prove FIFO order, winner balance, starvation freedom, or fairness over
all schedules; the committed test explicitly records those non-claims.

### 2. Current Core behavior

Identical shared-root, split-root, scaled lock-timeout/recovery, and bounded-race
results; corresponding Core reservation and SQLite seams are
`app.py:9116–9236` and `db.py:319–477`.

### 3. Why it is pending

Filesystem locking is simple and sufficient under a single canonical archive,
but correctness then depends on topology. DB-scoped coordination changes
contention/error semantics. Fair deterministic ordering conflicts with a cheap
first-lock-wins model.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve current lock and make one shared storage root a hard startup invariant | Split-root/HA users must reconfigure; winner remains nondeterministic | Existing duplicates need audit/release |
| Use a DB-scoped per-project mutex or `BEGIN IMMEDIATE`, recheck, then insert | Contention timing changes and DB availability becomes the coordination dependency | Resolve existing conflicts deterministically |
| Add DB invariants plus serialized glob-overlap evaluation | Migration and application-level overlap logic remain necessary | Duplicate losers need quarantine/release |
| Dedicated coordinator/advisory-lock service | Local-only simplicity and deployment independence are lost | Requires importing/reconciling current leases |

Non-binding lean: a DB-scoped project mutex plus in-transaction recheck removes
the split-storage correctness hole without asserting a named winner.

### 5. Test that fixes the choice

Use two-process barriers with shared DB/archive and reversed launch orders:
exactly one grant, one conflict, and one active row; do not assert the winner's
name unless fairness is selected. Repeat with one DB/different roots: either
one winner or a fail-fast topology error. Cover exact/glob races, same-agent
reacquisition, before/after-timeout DB locks, SIGKILL lock-owner recovery, and
a legacy overlapping-row reconciliation fixture.

## D11 — retire with active reservations or unread messages

### 1. Observed live behavior

Seed Blue with one active reservation, one unread message, and two signal files,
then retire it. `retired_at` is set, while the reservation, unread receipt, and
signals remain. A retired Blue can still `fetch_inbox`; `limit=1` returns the
pending message and clears both signals. New sends to the retired recipient are
rejected. Frozen retirement only sets the tombstone around `app.py:5020–5057`.

The committed race probe adds two stronger observations. A reservation for
Blue succeeds and remains active whether retirement pauses the call immediately
before or immediately after reservation creation. A send paused after recipient
validation succeeds after Blue retires, persists both its direct and BCC
recipient, and emits only Blue's signal; the same send paused before validation
is rejected after retirement with no message or signal. Frozen live and Core
match in `packages/agentstack_mail/tests/test_pending_decision_d11_d12.py`.

### 2. Current Core behavior

Identical around Core `app.py:5043–5080`, `:6360–6367`, and `:7563–7638`,
including the committed reservation/send races.

### 3. Why it is pending

Reversible soft retirement favors preserving state. Peers need immediate lease
handoff, senders need disposition of unread/ack-required work, and operators
need a predictable one-call versus drain/force model.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve everything until ordinary TTL/read/ack | Peers remain blocked and senders may wait indefinitely | None |
| Atomically retire, release leases, and cancel/dead-letter pending notification work | Exact resume/unretire semantics change; senders need visible disposition | Retired legacy rows require cleanup policy |
| Guarded two-phase retirement: reject until drained, plus explicit force mode | Current one-call automation and abandoned-agent cleanup need force | Existing state remains inspectable |
| Transfer leases/pending work to an authorized successor | Adds successor auth, audit fields, and conflict resolution | Ownership/provenance migration required |

Non-binding lean: guarded retirement with explicit preserve/release/transfer
force semantics makes the destructive choice visible.

### 5. Test that fixes the choice

Seed exclusive lease, unread normal and ack-required messages, and multiple
signals. Assert the chosen disposition, new-send rejection, retired fetch/ack
policy, and unretire behavior. Race retirement against send/reserve. SIGKILL
between tombstone and cleanup/transfer must converge on restart. Include a
legacy retired row with all pending state without deleting message history.

## D12 — signal cleanup after crash, retirement, or stale consumer

### 1. Observed live behavior

Message DB/archive persistence precedes best-effort signaling. SIGKILL exactly
at `emit_notification_signal` leaves the message and recipient plus archive
Markdown, but no signal file. Signal write exceptions are suppressed and there
is no durable outbox. Retirement leaves signals unchanged. Any `fetch_inbox`,
including `limit=1`, clears all signals for that agent; a two-message probe
returned one item and deleted both files. Frozen seams are around
`app.py:4524–4702`/`:7531–7606` and `storage.py:3113–3242`.

The shell watcher retries failed work after 30 seconds and stale leases after
120 seconds. A committed hermetic probe executes the exact watcher functions
with a fake external command and literal SIGKILL at four boundaries. Death after
the fake injection but before success recording leaves signal and lease; after
lease expiry it injects again, demonstrating the duplicate window. Death after
success recording leaves a signal that `state_should_attempt` permanently
skips; lease presence distinguishes death before versus after lease release.
Normal completion removes both signal and lease. The production source order
`submit < success record < lease release < unlink` is pinned in
`packages/agentstack_mail/tests/test_pending_decision_d11_d12.py`.

The remaining unobservable boundary is whether a real tmux-like external system
has applied submitted bytes immediately before process death. A successful
command return is the strongest hermetic boundary; no local watcher state can
prove the external side effect's instant. There is no server-side TTL cleanup
for pending signals.

### 2. Current Core behavior

Identical crash, cleanup, and hermetic watcher-state results around Core
`app.py:4547–4725`/`:7631–7638` and `storage.py:3115–3244`.

### 3. Why it is pending

Best-effort signals keep send available, while durable wakeups require an
outbox/consumer protocol. At-least-once recovery conflicts with at-most-once
external injection. Offline retention conflicts with bounded cleanup, and a
global dirty-bit clear conflicts with filter-aware message consumption.

### 4. Options and breakage

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Preserve best-effort files and global clear | Missed wakeups, duplicates, and stale files remain contractual | None |
| Transactional DB outbox per recipient with lease, ack, and dead-letter | Schema, worker, and API change; external injection still needs idempotency | Import or quarantine existing signals |
| Atomic file claim protocol with rename/lease/requeue, message-ID dedupe, and sweeper | Cannot guarantee exact-once across external side effect plus crash | Existing files can be imported; corrupt ones need quarantine |
| Derive wakeups from unread DB state; treat files as rebuildable cache | More polling/DB access and weaker edge-trigger behavior | Existing files cease to be authority |

Non-binding lean: use a durable outbox with an explicit at-least-once contract
and message-ID idempotency; do not promise exact-once tmux injection.

### 5. Test that fixes the choice

SIGKILL at recipient commit→notification persistence, temp write→publish,
consumer claim→inject, and inject→ack/delete. Restart must recover the proven
committed-message/no-signal state. Race two consumers, exercise lease expiry,
and pin the documented duplicate/idempotent outcome. Cover retirement,
filtered/limited/empty fetch, stale retention, corrupt/legacy files, and
per-message BCC privacy. Only selected notification IDs may clear unless the
decision explicitly chooses global clear.

## Decision procedure

For each item, the decision owner records the chosen option (or a new one),
data migration/disposition, rollout compatibility policy, and exact committed
test. Only then may that manifest entry change. A green Behavior differential,
this packet, or a recommendation above is not authority-cutover approval.
