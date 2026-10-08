# Candidate migration of Mail and delivery state

[日本語](namespace-state-migration.md)

PR3 prepares conversion and recovery of isolated snapshots. Activation stays false. Public tools, installer, legacy DB schema, delivery daemon, hooks and dashboard API 12 retain their existing behavior. No installed database, environment, service or pointer is switched. It connects the [planning contract](project-removal-plan.en.md) and [absolute reservation candidate](absolute-reservations.en.md) to persistent stores.

## Inputs and decisions

Supply all seven paths explicitly in `namespace_state_io.SourceBundle`: Mail DB, delivery DB, archive, signals, history, bindings JSON and config JSON. There is no installed-database discovery. Inputs require the current Mail schema with a Mail instance ID and legacy delivery v1; unsupported schemas stop. The read-only `namespace_transform.plan_report(source, choices)` reports complete conflicting agent ID groups, unresolved windows and active leases, reply errors and delivery ambiguity without tokens or message bodies. Unresolved decisions stop migration.

| choices | Decision |
| --- | --- |
| `rename_map` | Explicit name per colliding agent ID, including retired agents; no automatic winner |
| `window_map` / `window_agents` | Keep, generate or explicit UUID per window row ID; explicit owner ID if its legacy display name is ambiguous |
| `window_targets` | Previously issued UUIDs when making a replacement plan; normal resume uses the receipt |
| `lease_anchors` / `as_of` | Absolute cwd per active relative lease ID and the expiry reference time |
| `reply_map` | Explicit parent ID or null per invalid, missing, self-referencing or cyclic numeric legacy thread |
| `delivery_map` | Agent ID for an ambiguous historical delivery key, checked against message recipients |
| `wake_recovery` | Legacy leased delivery keys whose unfinished wake has been checked |

`delivery_key(project, name, message_id)` encodes a compact JSON array string; omitting the message ID produces the policy key. It avoids ambiguous newline separators. Config `delivery_policies` supplies `version`, `coalesce_seconds`, `base_backoff_seconds` and `max_backoff_seconds` per policy key. Settings absent from the old DB are never guessed.

Config `expected_writers` fixes the complete planned writer set, including Mail, delivery, archive, signals, lease GC and retention. Fixtures explicitly use `instruction_blocks: []`. A missing managed-block collector is not considered verified; installations with managed blocks require the later collector before real activation.

## Preservation and verification

Mail retains numeric agent/message/recipient/lease/window IDs, Mail instance ID, owner tokens, credential generations, read/ack state, to/cc/bcc, bodies, attachment JSON, audits and auxiliary tables. `legacy_projects` and `legacy_*project_id` retain provenance. Those provenance columns accept null for new global messages and reservations. Summaries remain historical with `cache_valid=0`.

Numeric legacy threads become direct `reply_to` edges; named values retain a read-only source and label. Missing parents and cycles require explicit repairs. Reply and purge serialize through the same SQLite write transaction. Ancestors of surviving children are held; a fully expired chain is removed together. New replies reject deleted parents and check parent-specific participants plus the mandatory delivery policy callback.

Delivery uses `(mail_instance_id, agent_id, message_id)`. It preserves all five statuses, attempts, lease owner/expiry, errors and created/updated/delivered timestamps, adding snapshotted policies and due times. The old table and trigger remain historical. Database constraints prohibit reopening terminal delivery. Tests cover expired lease recovery, changed owners and backoff. Original timestamp spelling is retained; comparisons use UTC instants.

Archive copies map stable agent IDs and historical project IDs. Attachment mapping binds original and new paths with hashes. The final source snapshot retains the complete original archive including Git history. Candidate archive copies are verified; a new Git writer is deferred. History and legacy signal bytes survive. Separate active signal envelopes use Mail instance ID, agent ID and message metadata. Broad legacy signals set `needs_inbox=true` without inventing a message. Bindings verify stable IDs and owner tokens and apply the resolved window UUIDs.

Initial and final verification compare every table row/column, FKs, FTS content and index integrity, attachments and all file hashes, and every delivery status. The [SQLite integrity-check](https://www.sqlite.org/fts5.html#the_integrity_check_command) runs only on the candidate. Read-only source connections and SQLite backup include committed WAL data.

## Phases and recovery

`NamespaceMigration(source, workspace, choices).resume(fence=collector)` writes only an owned private workspace. Source overlap, symlinks and unowned existing targets are rejected. Receipts and pointers use fsync and atomic replace under an exclusive lock. Outputs contain credentials; treat them as private snapshots.

1. `planned`: Resolve collisions and persist UUID decisions and the writer roster.
2. `prepared`: Verify the initial source generation and snapshot it.
3. `prevalidated`: Convert and compare the candidate.
4. `quiesced`: Require `FenceEvidence` for all writers stopped, common authority, supervisor restart disabled, closed connections and zero in-flight operations.
5. `final_verified`: Capture final changes, rebuild and revalidate everything; bind source/candidate/writer generation and evidence to the gate.
6. `switched`: Update workspace `candidate-pointer.json`.
7. `committed`: Recheck the same gate and pointer, then finalize the receipt.

This is a workspace switch rehearsal, not a service cutover. The caller supplies trusted fence evidence; PR3 does not stop OS writers. Fixture evidence must not be reused for production. Changes in writer roster or managed-block scope fail closed. Source changes before the initial snapshot require a new plan. Changes after final validation stop the gate.

Tests forcibly terminate the process before and after every phase and immediately after pointer replacement. Resume with the same inputs and workspace reuses receipt UUIDs and resolutions. `status()` reports phase, verification counts and rollback eligibility. `rollback()` restores only the workspace pointer, and only while the entire candidate is unchanged. New Mail/delivery/reservation writes or file changes require a forward fix or export instead.

## Explicit candidate usage and remaining work

Construct `CandidateMailStore`, `PersistentReservations` and `CandidateDelivery` with explicit candidate paths and expected generations. Stale writers are rejected. The delivery policy callback is mandatory. Candidate Mail writes currently cover the DB only. Archive/signal transport writers, public tool/proxy wiring, service stop/restart, installed pointers, managed blocks and real CLI cutover remain for later PRs. The existing 25-tool contract remains intact.

The unresolved #232 restriction rejects non-ASCII paths on case-insensitive filesystems. Expired unknown leases remain historical; active unknown leases block migration. Resolve this limitation before activating installations with Japanese paths on Mac.

Tests start from legacy-schema fixtures in temporary HOME directories without reading production state. Wheel, sdist and an installed candidate independent of checkout imports are verified separately.

A mismatch in pointer authority, receipt ID, generation or activation stops even a committed resume. If rollback is interrupted after pointer replacement, repeat `rollback()` while the candidate is unchanged to finalize the receipt, then resume.
