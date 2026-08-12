# AgentStack Mail remaining blockers — PluckyEinstein checkpoint

> **Frozen historical checkpoint:** this file records the state at candidate
> `3e6062d` and is not the current cutover status.  The non-normative blocker
> digest in `docs/agentstack-mail-cutover.md` is the current progress view;
> do not reuse the six-item remainder or estimate below as a live plan.

Canonical decisions: AgentMail #8379, #8381, #8387. Audit baseline: clean
`5ff73d9cda3fac9a032fb73c05d2639514e2a608`. Status vocabulary is exactly
`済 / 未 / 検証不能`.

This checkpoint travelled in its ledger-reflection candidate. Its exact clean
commit SHA was recorded in the external commit receipt after that commit
existed; this frozen document deliberately does not acquire a current or
future candidate SHA.

This file is a frozen checkpoint, not a second cutover authority. The current
runbook and its routed machine contracts remain normative.

## Parent's original six items

| # | Item | Status | Exact remainder | Estimate |
|---:|---|---|---|---:|
| 1 | env-branch placeholder four kinds | **済** | Env and targeted-tmux branches share one validator. Env-derived `pending-*` / `warm-*` / `claimed-*` / `mail-watcher` controls return rc2 with zero HTTP; a copied-resolver mutation proves the negative assertion is live. | 0h |
| 2 | fixture-owned `approved_base` | **済** | The fixture is the sole value source. Artifact verification byte-matches that fixture, distinguishes missing/unfetched objects from persistent-ref-unreachable objects, ignores temporary refs, and the workflow uses `fetch-depth: 0`; missing and unreachable mutations are covered. | 0h |
| 3 | 0004b EXIT option B product integration | **済** | Product path accepts a loopback-local retire without the target token, retains schema compatibility, and emits credential-free audit. | 0h |
| 4 | `rollback-revert-procedure` contradiction | **済** | Pre-cutover scope now contains only pre-authority abort and partial-client rollback before the first durable new-authority write. The post-authority reverse transform is preserved as a separate non-blocking post-cutover record. Evidence-handler closure remains a separate task below. | 0h for contradiction |
| 5 | seven environment-difference records | **済** | Six triggered non-blocking post-cutover environment records, the current approved-base persistent-ref activation requirement, and the separate all-environment producer/verifier defect are in the canonical fixture and digest-checked by the artifact verifier. | 0h |
| 6 | working-tree migration command | **済** | `copy`, `verify`, and `rollback-assess`, baseline-A, six snapshots, exclusions, and recovery tests exist. Production evidence-gate closure is tracked below, not counted as missing command implementation. | 0h |

## Approved 19-task split

ProOpus #8387 approved seven narrow pre-cutover tasks and twelve deferred
tasks. The broad installer, coexistence, installed-wheel, and documentation
tasks moved post-cutover only after their first-cutover minimum conditions were
absorbed into the seven pre-cutover contracts.

### Pre-cutover: seven

| Task | Harm criterion |
|---|---|
| `reservation-probe-safety-release-gate` | 1, 3 |
| `http-cli-transport-entrypoints` | 2, 3 |
| `service-lifecycle-supervision` | 1, 2 |
| `mcp-client-reregistration-cutover` | 2, 3 |
| `data-migration-reconciliation` | 1, 2 |
| `rollback-revert-procedure` | 1, 2; first durable write before only |
| `notification-layout-consumer-compatibility` | 3 |

### Post-cutover: twelve moved tasks plus two separate records

The twelve moved IDs are:

1. `d2-d3-worker-progress-diagnostics`
2. `d2-d3-timeout-process-group-cleanup`
3. `d10-diagnostic-liveness-timeout`
4. `provenance-regression-sync`
5. `reservation-performance-release-gate`
6. `installer-core-integration`
7. `coexistence-fault-soak-gates`
8. `full-performance-load-soak-matrix`
9. `full-repository-release-gate`
10. `installed-wheel-contract-release-gate`
11. `cutover-evidence-provenance-gate`
12. `cutover-documentation-consistency`

`post-authority-reverse-transform` is a thirteenth post-cutover record created
by splitting the old rollback task. It is retained for returning durable
post-baseline records to a legacy-authority copy after days of operation; it is
not permission to revert after the first durable write during initial cutover.

`client-key-rename-and-stale-selector-cleanup` is a fourteenth post-cutover
record. First cutover preserves Claude `mcp-agent-mail` and Codex `agent-mail`;
the optional rename, stale permissions, legacy raw curl selectors, and dormant
`deregister_agent` option are reviewed separately.

Six additional environment-difference records bring the post-cutover list to
20. Each has an explicit activation condition. The approved-base persistent-ref
item is a current gate activation requirement rather than a post-cutover task,
and the producer/verifier mismatch remains a separate all-environment contract
defect.

Read-only machine-local observation at **2026-08-11T15:18:49 JST** found 16
Claude settings files (global 1 + local 15), 68 allow occurrences, and two hook
matchers. The 24-tool boundary intersects 47 raw allows / 24 unique tools;
stale selectors are 21 raw occurrences / 10 unique absent tools. The earlier
69-selector count missed
`05_Agents/Life/attachments/.claude/settings.local.json` because the scanner
respected `**/attachments/` in `.gitignore`; the file predates this work. These
values are not a cutover-time authority. The future one-run collector must
include hidden and ignored paths, and must prove both that search works and
that a known ignored-path control is present before sealing its timestamp and
digests.

## Narrow pre-cutover re-audit

Result: **済 1 / 未 6**. The six `未` tasks all contain substantial implemented
components; their binary state remains `未` because a first-cutover minimum or
candidate-bound end-to-end proof is absent.

| Task | Status | Implemented now | Exact remainder | Estimate |
|---|---|---|---|---:|
| `reservation-probe-safety-release-gate` | **済** | Timeout, Git error, and filesystem-incomplete observations stay active; explicit TTL expiry releases. The candidate contains the semantic tests and the raw validator with adverse mutations. | Generate and bind the final-candidate raw safety artifact. | 0.25–0.5h at final evidence pass |
| `http-cli-transport-entrypoints` | **未** | Console entrypoint, 24-tool in-process/installed contract, loopback and root isolation exist. | Enforce passthrough on the direct CLI too; install the exact candidate wheel, start real isolated HTTP, verify health and 24 tools, keep legacy 8765 reachable, stop cleanly, and prove partial failure cleanup. | 1.5–3h |
| `service-lifecycle-supervision` | **未** | Render/start/status/stop, ownership, state-root lock, signal forwarding, process-group reap, static legacy-root rejection, dirty-tree isolated SIGTERM clean shutdown, and forced-kill residue/recovery probes exist. | Bind sealed lifecycle receipts to the clean exact candidate; then pass stop→stopped→start→bounded-health under normal/crash/duplicate cases using the real controller CLI sequence. | 1–3h |
| `mcp-client-reregistration-cutover` | **未** | Reversible whole-set CAS apply/rollback and repo strict-hook contract exist. The working candidate separates provider identity from client keys, preserves Claude/Codex keys and selectors by default, supports explicit rename, and removes endpoint→key coupling from generated child configs. | Make Orrery/dashboard selectors fail closed; implement a one-run hidden/no-ignore sealed inventory with separate search-liveness and ignored-path-completeness controls; include six stopped child configs without deleting resume state; obtain preview approval; managed restart/rebind all clients; deploy the strict hook; prove zero client writes old authority. | 6–12h plus maintainer preview |
| `data-migration-reconciliation` | **未** | DB/signals/archive copy, `.git` and runtime exclusion, unrelated baseline root, full-row/schema/PRAGMA/relation verification, collision guards, and rehearsal machinery exist. | Run production-shaped full working-tree/attachment reconciliation rather than the one-file synthetic archive/signals seed, and add the candidate-bound evidence handler/index record. | 2–4h |
| `rollback-revert-procedure` | **未** | C3–C5 assessment, C6 hard no-go, consumer exact rollback, cold restore, and R1–R5 runbook mechanics exist. | Rehearse pre-authority abort and partial-client rollback as one sequence, restore prior client/service state, show exactly one writer at each checkpoint, and add the separate evidence handler. | 2–4h after service/client closure |
| `notification-layout-consumer-compatibility` | **未** | Per-message writer, dual-layout watcher/bridge code, and D12 durable-message/duplicate/delete component behavior exist. | Seal the real consumer/version inventory, run legacy and per-message N/N-1 transitions, switch the real signal root, and retain a candidate-bound transition receipt. | 2–4h; overlaps client work |

## Product decisions

D1–D6 and D8–D12 are all `selected + implemented`. They are not eleven
unresolved technical decisions. Their uniform `cutover_state: no_go` is one
external approval condition: after technical closure, maintainer approves the exact
candidate and selected set once. D7 remains the exact selected,
not-implemented, post-cutover `no_go` control.

## Other immediate work not represented by the seven task names

- **済** — The runbook states the selected loopback local-process behavior
  directly, without the ambiguous Option-B label, and the closed #2/#4
  checkpoint is synchronized in the ledger-reflection candidate.
- **未** — The final candidate must regenerate candidate-bound selected
  behavior, distribution, reservation-safety, and closed #2/#4 evidence
  (0.5–1.5h, partly overlapping the seven tasks).
- **済 in the working candidate** — The root runbook gate-ID contract was red
  at `5ff73d9` because three IDs appeared multiple times. The approved split
  now names all 14 IDs exactly once; the targeted contract suite passed at
  2026-08-11T15:00 JST. It must stay green after commit and in the final suite.
- **検証不能** — production execution and maintainer's preview/final approval are
  intentionally outside the implementation worktree and remain forbidden
  until the machine gate is green.

## Current schedule estimate

The six narrow pre tasks have a raw sum of roughly 15.5–31 engineering hours.
HTTP/service, client/notification, and rollback rehearsals overlap, so the
engineering critical path is roughly 12–24 hours. The current safe schedule is
therefore **2–4 focused workdays**, plus maintainer's preview and final
exact-candidate approval. This estimate excludes deferred post-cutover work and
does not authorize live cutover actions.

#codex
