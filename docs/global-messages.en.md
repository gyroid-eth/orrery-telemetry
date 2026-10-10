# Isolated global messages and notifications (S2b)

[日本語](global-messages.md)

S2b runs only with an explicit, completed schema4 candidate. The default legacy mode and its 25 tools, existing S1/S2a configurations, and activation=false remain unchanged. This does not switch the live Mail service, user HOME, dashboard, or watcher.

## Published capabilities

| New tools | Capability | Behavior |
|---|---|---|
| `send_message` / `reply_message` | `message_send_v1` | Stable recipient IDs; message, attachment blobs and operation receipt in one DB transaction |
| `mark_message_read` / `acknowledge_message` | `message_receipt_v1` | Recipient-owned first timestamps; acknowledge also sets read |
| `search_messages` / `fetch_topic` | `message_query_v1` | Visible ID-descending keyset pages; explicit attachment download through search only |
| `fetch_summary` / `summarize_thread` | `message_digest_v1` | Digest computed from visible messages at read time; no stored summary or LLM |

`message_attachments_inline_v1` and `durable_message_outputs_v1` describe inline DB attachments and durable message facts. The [wire fixture](../packages/agentstack_mail/fixtures/global-server-s2b.json) is the single source for schemas, management operations, reasons and transitions. S2b adds optional `before_id` to the existing list-shaped `fetch_inbox`. Fetch remains a pure read and never consumes a signal.

## Fresh preparation and startup

Use the source snapshot, writer fence and final gate from [S2a preparation](global-server.en.md#s2a-prepare-a-fresh-schema3-candidate), with a separate `s2b-candidates/<name>/candidate/` workspace. Preparation first proves preservation of existing rows, IDs and tokens, then proves the explicit attachment/blob and notification conversion delta. Old archive/Git/attachment bytes, delivery DB, signals and attachment map/JSON are grouped as immutable preservation material in `candidate/provenance/`, outside the new active signals directory. There is no ongoing archive Markdown, attachment-file or Git projection. A future operator export can generate a one-time bundle from the DB.

The explicit preparation plan has `kind=orrery-s2b-preparation-plan-v1` and `activation_enabled=false`. Its source_paths, isolation_root, candidate_name, choices, request_id and fence_evidence follow the existing preparation API.

```sh
.venv/bin/python -m agentstack_mail.global_prepare_s2b --plan "$plan"
env AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough \
  AGENTSTACK_MAIL_ENV_FILE="$fixture/missing.env" \
  .venv/bin/python -m agentstack_mail.cli --host 127.0.0.1 --port "$port" \
  --global-config "$fixture/s2b-candidates/one/candidate/server-config.json"
```

Use a dedicated 0700 temporary fixture root, private HOME and TMUX_TMPDIR, and a free loopback port. Do not use a live DB as source. Existing schema2/3 roots without the required receipt fail with `CANDIDATE_SCHEMA_REQUIRES_REPREPARE` before any SQLite connection; their file inventory and bytes stay unchanged. Developers who created candidates with unreleased master APIs must also prepare a new workspace from synthetic source, rather than modify the old root. Startup requires the completed schema4 receipt and matching config. Resume interrupted preparation with the same plan/request ID.

Create a **new client name** pinned to the new instance/candidate/epoch and current enrollment. Point `AGENTSTACK_CLIENT_CONFIG`, wrapper defaults, explicit hook configuration, and profile `client_config` at that same context. Never resend old pending work under a new binding. Follow the existing [old-root resolution and caller redirection procedure](global-server.en.md#when-schema2-is-refused). Actual S2b intent integration with client entrypoints belongs to 4b/4c.

## Commit, reconciliation and delivery

Message, recipients, blob relations, FTS, operation receipt and dirty pair are committed in one transaction. UUIDs are unique across tools for each owner. Lost responses replay the original receipt for the same UUID/intent; a different intent returns `REQUEST_ID_CONFLICT`.

An immutable success receipt reports `durability=db-committed` and `outputs_as_of_commit=pending`. It does not claim delivery. Use the health/inspect `signal` field for current reconciliation state. Top-level `status` describes only whether the server can accept requests. Delivery/reconciliation health is centralized in `signal.status` (degraded/red) and counts/age; a red signal does not block 4a client reads.

Consumption has **one column**, `notification_fact`: NULL means unconsumed, `imported-consumed` means consumed in legacy mode without an invented timestamp, and `delivered:<UTC>` records the consumer's actual first completion time. The worker, health and inspect share one SQL predicate combining this fact with read/ack and recipient activity. Management completion reads the same fact and returns already-completed for imported consumption.

Legacy notification truth comes from the remaining hint inventory and Codex App delivery rows, with read/ack, delivered, explicit unknown resolution, pending/failed, remaining hint, then absent inventory precedence. An old unread message with neither hint nor delivery row becomes imported-consumed, without backlog or wake. A new reply to an old consumed parent is a new row and receives ordinary notification. Leased/dead-letter rows and unidentifiable hints require explicit `choices.notification_resolutions`: exact hint map/discard or pair delivered/retry_after. Bind choices to `source_delivery_digest` and `source_signals_digest`; preparation refuses unresolved or mismatched choices instead of guessing.

The worker locks `signals/agents/` itself, then authority SH, then BEGIN IMMEDIATE. It derives desired output **inside that write transaction**, materializes only `signals/agents/<recipient-id>/<message-id>.signal` with fsync/rename/unlink, and deletes the dirty pair in that same transaction. Interrupted work re-derives the same pair. Blocked pairs do not busy-retry or stop other recipients; future backoff stays pending.

A non-cooperating process with the same UID can still replace names between checks and open/rename. As with S1, this is a boundary for cooperating writers using the common lock. Foreign formats, symlinks, multiple links and owner/mode mismatches found during inspection are blocked, never overwritten. `reconcile_outputs` is the explicit retry entrypoint.

After successful injection only, a same-UID consumer calls Unix management `delivery_completed`. Lost-response retries preserve the first timestamp. Unknown messages are no-ops with orphan-hint reconciliation; a known nonrecipient rejects the entire batch. Do not reuse the legacy watcher's direct unlink in global mode. Real consumer integration and activation checks remain in 4b/4c and PR7.

## Limits, visibility and deletion

New attachments use strict base64: at most eight, 256KiB each and 512KiB total. Distinct owner blobs default to 256MiB, configurable upwards. Reusing a blob does not add usage. Large legacy attachments remain preserved and readable as metadata/content_ref. Download bytes only through `search_messages(attachment_sha256=..., limit=1, include_bodies=false)`. The final MCP envelope budget is 1MiB including text plus structuredContent. An oversized complete item/blob returns `MESSAGE_RESPONSE_TOO_LARGE`; storage is never truncated. Reduce inbox limit or request metadata; oversized blobs await a future operator export.

The keyset upper bound is the **visible MAX** from the same read transaction, zero for an empty visible set. It reveals no invisible message ID/count and survives worker/read/ack/touch changes. Binding is checked once through the caller context, not duplicated in the cursor. New messages stay outside continuation pages, while purged rows are skipped. Reply graphs use visible vertices and read-time SQL, without a stored conversation namespace or server cursor state.

Internal purge reuses the existing ancestor-hold plan: parents of surviving children are retained. Deleted recipient pairs become dirty in the same transaction; attachment relations and unreferenced blobs are removed. Historical receipts retain hashes/binding without resurrecting bodies on replay. Old preservation artifacts are immutable and outside purge. The runtime drops the old attachment map/JSON and maintains only DB blobs/relations. The message-ID high-water survives purge. Schema4 also uses the recorded inspect/quarantine entrypoint for writer tracker mismatch; unknown writes are never automatically adopted.

| Legacy difference | Explicit S2b global behavior |
|---|---|
| Name/project routing | Stable owner and recipient IDs; project arguments ignored |
| BCC wake | Same signal rule as to/cc; other recipients cannot see BCC identities |
| Contact exceptions | S2a blocked/block_all precedence, no reservation-overlap override |
| Attachment paths, conversion, broadcast, automatic contact | Fixed refusal reasons |
| Thread writes and LLM summary | reply_to for replies; legacy labels read-only; read-time digest |
| Archive/Git | Immutable legacy preservation only, no ongoing second source |

S1/S2a inbox items are raw rows; S2b uses the shared message projection with `sender`, `to/cc/bcc`, `read_at`, and `acknowledged_at`. The outer list and pure read remain. Consumers in 4b/4c must follow the profile/capability.

PR3 rejects old delivery rows without recipients with `DELIVERY_MESSAGE_UNAVAILABLE`. S2b adds no separate enumeration or admission path. Imported attachment MIME types come from recorded JSON `media_type`, or the fixed `application/octet-stream` default.

Reply subjects trim the prefix, preserve an existing prefix case-insensitively, and otherwise join with one space. Only caller-supplied subject and subject_prefix inputs are bounded; derived subjects are not checked again, so long legacy reply subjects remain usable. `reconcile_outputs.more` reports only targets beyond the selection limit; use `blocked_count` for processed blocked targets.

## Verify it yourself

With a development venv, run serially, without `-n`. The fixture creates synthetic source, private HOME/TMUX_TMPDIR and a free port; it waits for actual HTTP health and the expected profile, not merely a socket file.

```sh
PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python -m pytest -q \
  packages/agentstack_mail/tests/test_global_server_s2b.py
```

| Check | Test / expectation |
|---|---|
| HTTP and Unix socket | `test_actual_http_eight_tools_inbox_and_management`: eight schemas, all tools, completion and replay |
| Commit interruption | `test_write_interruption_same_uuid`: one message for the same UUID at every boundary |
| Signal interruption | `test_signal_interruption_same_pair`, `test_clear_signal_interruption_does_not_reemit`: reconciliation and no re-wake after read |
| Imported notification | `test_imported_consumed_not_backlog_new_reply`: zero backlog, new reply notified |
| Attachment/quota | `test_attachment_blob_dedup_metadata_download_quota`: distinct usage and replay |
| Visibility and pages | `test_visible_keyset_ignores_other_owner_and_survives_mutations`, `test_1001_chain_keyset_never_loses_tail` |
| Preparation interruption | `test_fresh_schema4_preparation_interruption_restarts`: refusal before complete, same-plan recovery |
| Old-root preservation | `test_old_schema_root_without_receipt_is_unchanged_before_db_open`: zero DB opens, identical bytes |

S2c owns reservations and contact welcome; 4b/4c own client, hook/proxy and consumer wiring; PR7 owns stop/migrate/activate. Existing dashboard UI and watcher still use legacy mode. Future global dashboard health must consume the same health `signal` field instead of introducing another undelivered predicate.
