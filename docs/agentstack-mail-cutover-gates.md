# AgentStack Mail cutover gates

This runbook covers the implementation-train step that precedes any authority
switch. It does not approve a cutover and it never changes the normative
`product_decisions[*].cutover_state` values. Human approval remains separate.

## Hermetic automated gate

From a clean repository checkout with the development environment described in
`CONTRIBUTING.md`:

```sh
CANDIDATE_COMMIT=$(git rev-parse --verify 'HEAD^{commit}')
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  packages/agentstack_mail/scripts/cutover_gates.py --gate all \
  --candidate-commit "$CANDIDATE_COMMIT" \
  --output /absolute/absent/cutover-gates-result.json
```

The runner authenticates and reconstructs frozen live from the checked-in Git
bundle and patch. It does not consult a mutable AgentMail checkout. Every
writable database, archive, signal tree, home, CWD, and temporary directory is
under one private disposable root. Network services bind only dynamically
allocated loopback ports; ports 7333, 8765, and 8770 are refused.

The terminal JSON is `status=pass` only when all four gates and all four broken
controls pass:

- **coexistence:** frozen live and Core are alive simultaneously with disjoint
  state. Health is a write-free identity operation, each authority makes a
  positive write, and neither write changes the other authority. A shared-root
  configuration is the red control.
- **migration:** existing `copy_state` and `verify_copy` preserve exact schema,
  every table row/count, five relationship projections, archive, and signals.
  A same-root copy is a no-op. Changing one message sender relationship while
  preserving schema and counts is the red control.
- **rollback:** the verified C3/C4 pre-write baseline is reversible, Core can
  read the copy, and the restarted frozen-live authority passes health, known
  identity lookup, durable send, and inbox readback. A real post-baseline Core
  write must produce `no_go`. This gate does **not** claim a post-authority
  reverse transform: after the first durable Core write, initial cutover remains
  fix-forward-only.
- **fault:** D8 runs literal SIGKILL after one archive copy and an ordinary
  archive exception while requiring the database delivery row to survive. D10
  runs four shared-root two-client reservation races, each with one grant, one
  conflict, and one active row. D12 checks best-effort signal failure, BCC
  privacy, retirement preservation, fetch cleanup, and one watcher crash/retry
  window. Deleting the required D8 database row is the red control.

Before any measured success call, the HTTP result detector makes a deliberately
failing tool call and then a successful health call. It parses JSON and reads
the boolean `isError`; the presence of the string `"isError": false` is never
treated as failure. Every gate also contains a positive durable-write or fault
observation so an empty/no-op implementation cannot pass.

Run one gate during diagnosis with `--gate coexistence`, `migration`,
`rollback`, or `fault`. A one-gate pass is diagnostic only; the cutover input is
the fresh candidate-bound `--gate all` result from a clean exact checkout.

## Real-machine soak gate (manual; not executed by the automated runner)

Run this only after the exact candidate commit and wheel are fixed. The soak is
isolated rehearsal, not an authority switch: production clients keep their
current endpoint, the existing authority is not stopped, and the candidate must
not use the production database, archive, signals, launchd label, or ports 7333,
8765, and 8770.

### 1. Seal inputs and isolation

Record the full candidate commit, wheel SHA-256, Python version, machine power
state, start time, and intended duration. Use a private absent state root and a
free dynamically allocated loopback port. Before starting, fail if any
candidate database/archive/signal path resolves to or below a live authority
surface. Record `lsof` tables for the candidate port and both database families.

Run the automated `--gate all` command above against the same candidate first.
Keep its mode-0600 JSON and SHA-256 beside the soak log. If it is not a four-gate
pass with four detected controls, do not start the soak.

### 2. Start the isolated candidate

Use the candidate wheel's `agentstack-mail-service foreground` with an explicit
private env file and state root. The env file must select:

- the dynamically allocated non-reserved loopback port and `/mcp` path;
- `<soak-root>/storage.sqlite3`, `<soak-root>/archive`, and
  `<soak-root>/signals`;
- `AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough`;
- no bearer/JWT configuration;
- a fixed Git author and `AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC=true`.

After TCP readiness, first make one intentionally failing `whois` call and
verify parsed `isError=true`; then require health success with
`isError=false`. List tools and require the exact versioned 24-tool set. Use
dedicated synthetic project/agent names and registration tokens that are not
production credentials.

### 3. Exercise for 24 hours

Keep the Mac awake and on AC power. For 24 continuous hours, run a synthetic
cycle every five minutes (288 planned cycles): send one uniquely numbered
message, fetch/read it, reserve and release one unique path, and make one
read-only health/known-identity call. Record JSON result bodies, start/end
times, operation number, and latency; never classify by substring.

During the run, perform these bounded human-observed events once each:

1. normal SIGTERM and restart; the port closes, restart succeeds, and all prior
   messages remain readable;
2. SIGKILL during an archive-writing synthetic send; after restart, startup heal
   commits the surviving audit files and the D8 database row remains;
3. a two-client same-path exclusive reservation race; exactly one grant and one
   conflict result;
4. a notification-watcher crash after external injection, followed by lease
   expiry/retry; the documented at-least-once duplicate window is observed and
   the eventual successful retry removes signal and lease;
5. retirement with pending per-message signals, followed by the selected inbox
   fetch cleanup; retirement preserves the signals and fetch clears only the
   selected agent's signals.

After every restart and at least hourly, record SQLite `integrity_check`,
`foreign_key_check`, message/recipient/reservation counts, `git fsck --full`,
archive status after the async queue becomes idle, process-tree ownership, and
`lsof` isolation. The existing authority must remain available and its
database/archive/signal fingerprints must not change as a consequence of any
candidate operation.

### 4. Acceptance and cleanup

The manual soak passes only if all 288 cycles are accounted for; every planned
success has parsed `isError=false`; every intentional failure has parsed
`isError=true`; there are zero unexplained transport failures, foreign-key or
integrity errors, lost database messages, shared writable paths, production-port
uses, or candidate-caused changes to the existing authority; and all five
bounded events satisfy their exact expectations above.

Stop the candidate with SIGTERM and require process-tree exit and port closure.
Run final integrity/relationship/count/Git checks, write a terminal report with
the candidate and automated-gate pins, and have the operator review it. Preserve
the report before deleting the disposable soak root. A failed or interrupted
soak is not resumed or relabeled as success; start a fresh 24-hour run from an
absent root after fixing the cause.

This manual receipt is evidence for human review only. It does not change the
decision ledger, does not make the current readiness evaluator return `go`, and
does not authorize the authority switch described in
`docs/agentstack-mail-cutover.md`.
