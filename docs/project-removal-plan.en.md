# Project removal contract and migration plan (PR1)

[日本語](project-removal-plan.md)

This prepares [#213](https://github.com/gyroid-eth/orrery-telemetry/issues/213). The published 25 tools, project-scoped delivery, database, installer and hooks retain their current behavior. Candidate APIs require explicit calls and cannot activate live cutover.

## Candidate adapter

`namespace-tools-v2.json` and `NamespaceAdapter` accept and ignore the audited legacy scope arguments for each tool: `project_key`, macro `human_key`, and contact `to_project/from_project`. Their values do not affect candidate dispatch. Credentials and existing-owner recovery arguments are preserved. Unknown arguments are rejected. Candidate `ensure_project` is a no-op; the published tool still creates projects.

`dispatch(tool, arguments, backend)` requires an explicit candidate backend. That backend retains authentication, argument validation and contact policy. It is not an adapter that can be plugged directly into current project-required functions.

## Replies without thread identities

`ReplyCatalog` stores a `reply_to` message ID on each message. It does not issue thread UUIDs or maintain a thread namespace, mapping or alias resolver. Omitting the parent creates an independent message. `CallerEvidence` must come from a trusted authentication/binding layer, never tool arguments.

Numeric legacy `thread_id` values become references to preserved message IDs. They may represent old conversation roots; missing direct parents are not inferred. Named values become read-only `legacy_thread_label` metadata with source provenance. Labels, subjects and topic tags do not create edges. Missing parents, self references, cycles and invalid IDs block migration and restore.

Permission to reply comes from the original message's sender or individual to/cc/bcc recipients. Conversation membership does not replace this check. If message101 is A/B and message102 is A/C, B can reply to101 but cannot reply to102. A later recipient may also need to replace a numeric legacy root input with their own accessible message ID.

The candidate `send_message` adapter converts numeric legacy inputs to `reply_to`. Both inputs may be supplied only when they agree; otherwise it raises `REPLY_INPUT_CONFLICT`. Named writes raise `LEGACY_THREAD_READ_ONLY` with an update requirement. The backend's `ReplyCatalog.resolve()` checks the caller and parent.

Candidate `reply_message(message_id)` uses that ID as the direct parent. Its runtime wrapper, default recipients, subject prefix and importance/ack/topic inheritance are implemented later; the old thread inheritance is not carried into the candidate contract.

`summarize_thread` remains a compatibility entry accepting new `message_id` or old `thread_id`. Numeric values select reply components, comma-separated values select each starting point, and names select read-only historical labels. The backend must deduplicate IDs across `legacy_read()` views when aggregating. Publishing `summarize_conversation` and retiring the old name require client inventory checks. `fetch_topic` remains a topic-tag query.

`conversation()` traverses reply connections and returns only message IDs visible to the caller, plus a partial-history indicator and visible-result pagination indicator. It does not return hidden IDs, bcc, bodies or hidden counts. A rendering/summary backend must continue message-level visibility and bcc restrictions. Actual UI, body rendering and LLM summaries are outside this PR. Legacy label queries keep sources separate and do not claim they form one conversation.

`record_delivery()` is called after backend owner/contact checks. A plan-local lock keeps parent checks and registration together. Identical delivery replay is idempotent; changing an existing message's edge is rejected. Export/restore preserve IDs, sender and individual recipients, reply edges, topics and label/source metadata, and revalidate references. These inputs contain no message bodies or credentials.

## Purging parents while children survive

`plan_purge()` and `purge()` recursively retain ancestors referenced by surviving children. An old parent with a new child is held; an entirely expired component may be deleted. Children are not cascaded, edges are not nulled or redirected, and tombstones are not introduced. Candidate counts, dry-run deletion/hold counts and actual deletion/hold counts remain distinct.

Purge replans at execution: a reply added after dry-run holds its parent. If purge commits first, a later reply raises `MESSAGE_UNAVAILABLE`. This models the contract under an in-memory lock. Persistent write transactions, RESTRICT/NO ACTION foreign keys, deletion order and runtime purge integration belong to PR3/4.

## Names and windows

`plan_agent_names()` reports exact/normalized conflicts, including retired rows, without automatic renaming or merging.

`plan_windows(records, {12: "keep", 34: "generate"})` uses explicit row IDs for every conflicting row. Generated UUIDs are retained in the receipt and replayed through the same choices and `prior_targets`. Expired rows and bindings are preserved. Changed inputs or new duplicates require resolution.

## Final validation and the fixed writer roster

Planning phases are `planned → prepared → prevalidated → quiesced → final_verified`. PR1 cannot transition to `switched/committed`.

The initial snapshot's nonempty, unique `expected_writers` is pinned in the receipt's `writer_roster`. Quiesce, final validation and the current gate compare both evidence sets against it. Shrinking expected and observed writers together cannot pass. A different final snapshot roster requires replanning; restore retains the roster. The actual trusted inventory collector is implemented later.

After final delta application, all **13 checks** run again: names, windows, reply references/legacy labels, lease anchors, message IDs, recipient/read/ack, credential generations, attachments, FTS, delivery states, contact policy, bindings and block hashes. Trusted verifiers use `FinalValidationEvidence.record(snapshot, candidate, results)` to bind actual results to the examined data. Stale evidence, changed data, missing checks, unresolved inputs, writer restart/unknown state, open connections or pending wakes prevent eligibility. Correctness is not inferred from counts.

Even an eligible gate always returns `activation_enabled=False`; environment variables cannot enable cutover. Receipt JSON digests detect accidental changes, not authenticate callers or sign data. Persistence permissions, atomic publication, actual collectors, writer stopping and authority handoff are later work.

## Verification and remaining work

Temporary HOME fixtures test per-message permissions, numeric/label compatibility, references/cycles, visibility, both purge/reply orders, export/restore, name/window resolution, simultaneous writer-roster shrink, stale evidence and disabled activation. Real in-process v1 MCP calls against fixture state check that published schemas and project-scoped inboxes remain unchanged.

This PR does not migrate live Mail/delivery databases, implement persistent FK/transactions, filesystem activity/GC, service cutover, installer/runtime/client changes or block/UI migration. Fixture success does not establish live migration success or authentication enforcement for every raw tool. There is no new operator migration CLI yet.
