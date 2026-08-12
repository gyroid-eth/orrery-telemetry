# #2 / #4 fixed requirements — PluckyEinstein closure checkpoint

Canonical source: AgentMail #8334, #8335, #8340, #8376, #8379. Status vocabulary is exactly `済 / 未 / 検証不能`.

## #2 C6 hard no-go — 8 items

1. **済** — Same fresh manifest: C5 returns rc0 + one JSON, reversible, both baseline matches true, data reversible true. Positive control is in the same test as C6.
2. **済** — Same manifest C6 returns rc1 + one JSON, policy/durable-boundary no-go even while both baseline matches remain true; no legacy-start/client-before-image action.
3. **済** — No intermediate stage or alias. `C5_TO_C6` and `C6_CUTOVER_COMPLETE` are both rc2, stdout empty, argparse invalid choice. `C6_NEW_AUTHORITY_VERIFIED` is the one canonical C6 name in code and runbook; the temporary alias was introduced by PluckyEinstein without an external compatibility need and has been removed.
4. **済** — Omitted `--cutover-stage` is rc2, stdout empty, argparse required error.
5. **済** — C0–C2 are not accepted by `rollback-assess`; each is rc2/stdout empty. Their rollback is a separate runbook path.
6. **済** — C3/C4/C5 source and destination one-record drift are a 3×2 negative matrix; all are rc1/no-go and do not authorize legacy/client rollback.
7. **済** — Every valid-stage JSON marks stage as `caller_asserted_unverified` and requires external service/client verification; help states the tool does not infer stage/service state.
8. **済** — Mutation-sensitive boundary: removing C6 hard guard makes fresh C6 red, while the same-test C5 control prevents an all-stages-no-go false fix.

Residual declared limit: a caller can falsely label a post-write state as C5. The tool cannot observe service/write history, so the runbook makes the first durable write immediately C6 and keeps the caller-asserted marker.

## #4 restore rehearsal — 6 initially missing items

1. **済** — Built-in damage owns fault-before/fault-after artifacts under one run UUID, changes both physical and logical state, and refuses no-op before restore. Caller free text is not the rehearsal evidence.
2. **済** — Terminal receipt/raw artifacts retain exact source/backup/damaged/restored main/WAL/SHM presence, size and SHA maps. Restore physical state is measured before SQLite reopen and post-logical state is recorded separately.
3. **済** — Clean candidate `5ff73d9cda3fac9a032fb73c05d2639514e2a608` produced the production-shaped runner, separate verifier, and non-mutating check-only receipts. Their 2026-08-11 verification-time location was `/private/tmp/agentstack-mail-evidence-5ff73d9c`; that temporary path is not a durable artifact address. External generator/rehearsal/verifier pins and retained raw command observations were independently recomputed by HappyTesla and ProOpus.
4. **済** — Backup/restore/rehearsal publication code has file/temp/receipt/replace-or-unlink/parent-directory fsync paths and injected-EIO tests that leave no canonical success receipt. **検証不能:** receipt text cannot prove the storage device physically flushed; only code path + fault behavior are claimable.
5. **済** — Restore-skip, PRESENT-replace-skip and ABSENT-unlink-skip mutants are red. The exact “already-diverged target + actual damage function no-op” control is also red before restore and leaves no canonical success receipt.
6. **済** — Separate verifier requires external receipt SHA/run/candidate pins and recomputes retained raw/logical evidence. Its write-once receipt is externally pinned; repeated `--check-only` revalidation writes nothing and detects raw/receipt changes.

Execution-proof limit: **検証不能** cryptographically against a producer allowed to fabricate receipts and raw files. The accepted upper bound is candidate-bound write-once receipts + externally held pins + retained raw artifacts + independent recomputation + negative controls.

## ProOpus seed-scale addition

- **済** — The deterministic fallback seed was 72,007,680 bytes with 800 agents, 8,200 messages/recipients, and 2,000 reservations, larger than the measured 63,430,656-byte live DB. Source and restored main files were byte-identical; damaged was an unreadable 3,904-byte non-database; absent WAL/SHM sidecars were created by damage and removed by restore. The full-scale rehearsal, verifier, and check-only receipts passed independent recomputation.

The #2/#4 defect closure is bound to `5ff73d9`. A later final candidate must regenerate candidate-bound evidence, but that routine rebinding does not reopen these semantic requirements.

Repository documentation review for this closure covered the package README,
Claude/Codex runtime guidance, and the root README pair. The root README pair
describes the currently shipped legacy AgentMail release rather than claiming
that AgentStack Mail is already the production authority, so it remains
unchanged until the installer/cutover release changes that fact.
