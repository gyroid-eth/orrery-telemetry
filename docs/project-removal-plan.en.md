# Namespace contract and migration planning (PR1)

[日本語](project-removal-plan.md)

This is the first preparation for [#213](https://github.com/gyroid-eth/orrery-telemetry/issues/213). **Published tools, project-scoped routing, databases, installers and hooks keep their existing behavior.** The candidate APIs require explicit fixture/planning calls and are not connected to live cutover.

## Included components

- `namespace-tools-v2.json` freezes candidate input schemas for the 25 audited tools without changing the published v1 fixtures. Legacy scope arguments are listed separately for each tool.
- `NamespaceAdapter` accepts and ignores audited project/human/contact scope arguments. Different values and omission reach the same candidate dispatcher. Credentials and other arguments are preserved. The backend retains operation-specific ownership and value validation; unknown arguments are rejected.
- `ThreadCatalog` issues IDs, resolves legacy inputs and message replies, and plans confirmed group/separate decisions. It opens no database. Plan-local uniqueness is enforced; persistent uniqueness and live tool wiring belong to later PRs.
- Agent/window planners detect exact/normalized collisions, including retired rows, and require explicit window decisions by row ID. They do not choose collision winners or merge agent owners.
- Receipts, final-validation evidence and writer-fence gates make replay and cutover preconditions executable. Collecting evidence, validating actual data, saving files and stopping writers are later work.

## Candidate dispatch and thread continuation

The adapter requires an explicitly supplied candidate backend. Do not connect it directly to the current project-required tool functions. Candidate `ensure_project` is a no-op; the published tool retains its existing behavior.

Scope removal does not remove authentication. `CallerEvidence` must come from the trusted binding/authentication boundary, never caller-provided tool arguments. Preserve raw/proxy/operator guarantees separately.

New threads receive `th-<UUID>` IDs; equal labels do not identify the same conversation. Resolve registered canonical IDs before confirmed legacy aliases. Legacy aliases with canonical syntax remain resolvable, and issuance avoids alias collisions. Replies use preserved message IDs.

Repeated aliases across old sources require confirmed group/separate decisions: some are independent conversations, others existing cross-project deliveries. Similar content or timing is not proof. A confirmed group preserves a shared conversation; separate decisions preserve individual histories. Unknown relations can be retained separately with legacy continuation marked as requiring an update, while message replies remain available.

Resolve aliases only with confirmed caller binding/participation evidence and a unique target. Project arguments never enter the resolver. Ambiguous, unknown and relationship-unknown inputs return fixed diagnostics rather than creating another thread. Unconfirmed callers and inaccessible messages are rejected. Export/restore preserves allocated IDs and validates mappings; trusted backend delivery records extend participants only after existing owner/contact checks.

## Window decisions

Specify row IDs, for example `plan_windows(records, {12: "keep", 34: "generate"})`. Duplicate UUIDs need explicit decisions for every affected row. Store generated targets in the receipt and replay the same choices with `prior_targets`; changed inputs require a new plan. Keep expired rows and bindings. New duplicate targets remain unresolved.

## Final validation and gating

Planning phases are `planned → prepared → prevalidated → quiesced → final_verified`. Reserved future phases `switched/committed` cannot be entered in PR1.

After final deltas, rerun all 13 checks: names, window/thread mappings, lease anchors, messages, recipient/read/ack state, credential generations, attachments, FTS, delivery state, contact policy, bindings and block hashes. A later trusted data verifier records results bound to the exact source/candidate digests using `FinalValidationEvidence.record`. Initial evidence cannot be reused for a changed final snapshot. Fixture results are not live validation evidence, and counts do not prove FTS or ownership correctness.

The fence requires common authority, suppressed supervisors, every expected writer stopped, closed connections and zero known inflight wakes. Empty inventory, unknown state or writer reentry blocks readiness. Acquiring authority and collecting observations are later implementation work.

The gate compares the final receipt with current source, candidate, resolutions and fence. Changes, unresolved inputs and missing checks block readiness. **Even a ready gate has `activation_enabled=False`; no environment variable enables PR1 cutover.**

Receipt JSON preserves non-secret resolutions and snapshot digests. Its integrity digest detects accidental changes, not authentication or tamper-proof signatures. File ownership and atomic publication belong to later PRs.

## Validation and remaining work

`test_namespace_contract_plan.py` runs candidate contracts, collisions, resolution/replay, independent/shared threads, stale final evidence, writer reentry and disabled activation under a temporary HOME. An in-process v1 MCP test uses a fixture DB to verify unchanged published schemas and separate project inboxes.

Live DB consolidation, Codex App delivery DB conversion, activity/GC, service cutover, legacy instruction cleanup/UI and installer/client updates are excluded. This release does not publish a migration CLI. Existing quick-start instructions and managed blocks therefore remain unchanged.
