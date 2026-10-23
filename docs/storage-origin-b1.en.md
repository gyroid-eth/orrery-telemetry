# Storage-origin B1 (unpublished)

B1 is an isolated kernel for issue #213's selected S′ approach. The default server, installer, hooks, proxy and dormant global mode remain unchanged. This is not a live cutover or completion of #213.

## Implemented boundary

The kernel retains existing Agent/Message/MessageRecipient/MailInstance rows, numeric IDs, names, origins and unread state. Instance I, actor A, credential K/generation g and target T are separate. Owner verification and read/ack effects share `BEGIN IMMEDIATE`; an OwnerContext expires outside its task/session/transaction.

Current-name matching unions exact-lowercase and sanitized-lowercase matches across all origins, including retired rows. A unique active name is accepted; ambiguous names return candidates. Explicit IDs work independently of ambiguity. No latest-row/current-folder winner, rename, hard deletion, identity recycling, historical aliases or name tombstones are introduced.

Private full credential fingerprints retain historical ownership. Shared credentials remain burned through partial recovery/restart/preparation; rotated keys remain revoked. Preparation cannot bless damaged private history. Pure reads do not mutate domain state. Read changes only the owner's first read timestamp; ack atomically preserves the owner's first read and ack timestamps.

Accepted results and authenticated terminal refusals retain request/intent mappings. Same-content retries return the original outcome; conflicting content, missing mappings or missing result references fail closed. There is no mapping GC. Candidate cursors bind instance, ruleset, normalized query keys, revision and position; total counts precede pagination.

Candidate metadata is separate from SQLModel.metadata. Only explicit preparation creates it. Integrity validation scans all agents, credential history and request mappings; cost grows with history. Large-scale performance remains a pre-cutover check.

## Tests and deferred integration

`storage_origin_candidate.build_candidate_server(core)` is an explicit in-process backend factory using a supplied session factory. It has no CLI/HTTP entry or activation flag. Its backend auth envelope is not a model-facing proxy schema. Raw arguments are checked before coercion/defaults; omitted auth differs from NULL and failed proof never falls back to anonymous reads. The contract fixture is `packages/agentstack_mail/fixtures/storage-origin-b1.json`.

Use the development environment in [CONTRIBUTING.md](../CONTRIBUTING.md):

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q packages/agentstack_mail/tests/test_storage_origin_b1.py
```

Tests use isolated SQLite and actual ORM/handlers, not live databases, credentials or service ports. Race tests use healthy registration updates to agents, credential indexes and the source digest. The prelock-resolver mutant commits registration between resolution and the owner's write lock.

Send/reply return **decisions only** (`decision_only=true`), never a delivered/accepted message. Final writer, full contact policy, cross-origin queries/attachments, bound proxies and UI/window routing are B4. Absolute reservations and real Mac/WSL/Windows-side/Japanese-path acceptance are B3. The private updater's O/name/K projection preserves Mail ID; real v2 storage/publication/resume is B4/B5. Unrecoverable credentials retain their old ID in NEEDS_RESOLUTION until explicit user recovery; legacy census/audit interpretation and backfill are B2. Notification consumption validates ownership then reports NOT_READY; durable delivery D/owner consumption C and actual shell/App consumers are B5. Writer fences, cutover receipts and recovery are B2/B5.

No user-facing default behavior or dashboard endpoint changes, so README/install/hooks/user instructions and API generation do not change. B1 success does not establish live delivery, all-caller coverage, real filesystem support or end-to-end updates. Code revert remains sufficient before activation.
