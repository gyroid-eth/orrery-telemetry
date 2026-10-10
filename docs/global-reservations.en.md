# Isolated global reservations and contact welcome (S2c)

[日本語](global-reservations.md)

## Public contract

Legacy mode remains the default and `activation_enabled=false`. Existing S1/S2a/S2b profiles retain their behavior. Only an explicitly configured, completely prepared schema5 server exposes seven additional tools and extends `macro_contact_handshake`. The normative contract is [global-server-s2c.json](../packages/agentstack_mail/fixtures/global-server-s2c.json).

| Tool | Operation |
| --- | --- |
| file_reservation_paths | Atomic acquire |
| renew_file_reservations | Owner renewal |
| release_file_reservations | Owner release |
| list_file_reservations | Pure keyset read |
| check_file_reservations | Pure coverage read |
| macro_file_reservation_cycle | Atomic acquire and optional release of new rows |
| macro_start_session | Existing owner activity, acquire, inbox in one transaction |
| macro_contact_handshake | Existing tool extended with explicitly approved welcome |

The catalog contains 31 tools: the previous 24 plus seven. `macro_prepare_thread` is omitted; use `summarize_thread` continuation and `fetch_inbox` separately. `force_release_reservation` and `purge_messages` remain same-UID management-socket operations, never public MCP tools.

## One reservation authority

`file_reservations` is the sole lease authority. ACTIVE means `released_ts IS NULL AND expires_ts > :now_utc`. Expiration changes the derived predicate without updating rows, revisions or receipts. The PR2 in-memory engine, copy/flush adapter, activity probe and collector are removed. The legacy app's independent activity implementation remains unchanged.

The server requires absolute paths or exact virtual `tool://`, `resource://`, `service://` names. Clients normalize relative names using observed cwd and explicit WSL mappings; ignored project scope never anchors a path. Shared normalizer/overlap rules retain symlinks, missing files, globs, Unicode and spaces. Unknown filesystem/case rules cannot grant a conflicting lease. Non-ASCII case aliases on case-insensitive filesystems remain unsupported without a verified case table.

Acquire is all-or-nothing: overlapping owners conflict if either lease is exclusive; shared/shared coexist. An identical own ACTIVE pattern/exclusivity reuses its ID, preserves reason and extends expiry to the maximum. Expired history is unchanged and acquisition allocates a new ID.

Renew/release accept IDs XOR paths. Both selectors yield `LEASE_SELECTOR_CONFLICT`; explicit empty arrays yield `LEASE_SELECTOR_EMPTY`; omitted selectors select only the owner's ACTIVE rows. Path selectors select only the owner's ACTIVE rows with identical normalized patterns; no match returns LEASE_NOT_FOUND. Foreign and own historical rows never enter path selection. Missing or foreign IDs reject the entire batch. Renewal adds seconds to the previous expiry and rejects expired/released rows. Explicit owner release by ID may release expired rows. Limits are 100 paths/selected rows, 60–86400 seconds TTL/extension and 500 characters for a new reason. Imported long reasons/expirations remain readable and releasable/renewable. Shared 16KiB preimage and 1MiB response budgets also apply; an oversized macro result rolls back the entire transaction.

Listing uses bounded ID keysets, with tool/owner/query binding and a fixed first-page MAX. New IDs do not enter continuation pages; existing leases reflect each page's current expiry/release state. Coverage returns one minimum-ID witness per concrete path. Both reads preserve activity, quota and notification state.

`auto_release=true` releases only newly allocated rows, preserving reused own rows. Start-session updates only existing-owner activity, reservations and pure inbox reading in one transaction. It never registers identities, changes program/model/window or marks messages read/acknowledged.

Coverage accepts a known witness even when another ACTIVE row has unknown rules. Without a witness, unknown rules produce ACTIVE_LEASE_RULES_UNKNOWN, never a negative assertion. The session macro shares inbox selection with fetch_inbox and returns metadata with body_md=null; read/ack state remains unchanged.


## Differences from legacy mode

| Area | Legacy mode | Isolated schema5 |
| --- | --- | --- |
| Collision | Partial grants plus conflicts/holders | Atomic RESERVATION_CONFLICT for the whole batch; no conflicts array |
| Expired renewal | Extend from max(old expiry, now), reviving it | Explicit selection rejects LEASE_EXPIRED; implicit selection includes ACTIVE only |
| Default cycle reason | macro-file_reservation | Empty string; start-session retains macro-session |
| Auto-release | Releases specified reused own paths too | Releases only newly allocated leases |
| Expiry/activity/collection | Existing app activity/grace/stale sweep and expiry cleanup | Derived fixed-width UTC expiry; no activity probe or collector |
| Selectors/batches | Legacy tool shapes; PR2 candidate also allowed the union of IDs and paths | IDs XOR paths; omitted/path select own ACTIVE only; reject over 100 selected rows without counting expired history |
| Owner/scope/paths | Project/name and relative paths | Stable ID/token/binding, ignored project scope, absolute/virtual paths |
| Start-session macro | Project/registration/reservations/inbox composition | Existing owner only; touch/reserve/inbox in one transaction |
| Thread macro | Raw catalog macro_prepare_thread, outside namespace compatibility | Not added |
| Force/purge | Raw agent-catalog management tools | Same-UID management socket only |
| Welcome | Existing handshake request/send behavior | Request only until target approval, then a new UUID; blocked takes precedence |
| Inputs/imported values | Long reasons, offset timestamps and old IDs | New inputs: 100 paths, reason 500, TTL 60–86400; preserve old reasons/IDs and normalize timestamp instants only |
| Notifications/default operation | Existing project operation | One S2b signal path; default legacy, activation=false |

## Time, retirement and operator recovery

One helper converts created/expires/released timestamps to fixed-width UTC `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. CHECK constraints and startup roundtrip validation enforce this format. Only those constrained columns use SQL TEXT comparisons. The S2a fixture changes one prose line; schemas and rejection classifications remain unchanged.

Retirement releases ACTIVE rows with cause=retired. Already expired unreleased history stays NULL and inactive. Unretire never resurrects leases. Retired owners cannot authenticate replay; after explicit unretire, the same token/generation and UUID return the historical receipt. Read current state separately.

Before force, call existing management `action=inspect` with expected `agent_id` and optional `reservation_id`. Binding, lease and revision share one snapshot. Force requires expected owner/revision, confirm and note. After lost response or STALE, inspect current state; never silently replace expected revision or force again. Recovery compares revision/state, not release_note, and does not claim an exact historical force receipt. Management inspection permits retired owners.

Purge requires explicit cutoff, after_id, limit and dry_run. Shared ancestor holding, FTS, blobs and notification dirty handling run in the same transaction. Held-only pages advance next_after_id to the last scanned ID; stop when more=false. Immutable provenance is never purged.

## Welcome and interrupted operations

Unapproved contacts return request-only and do not queue welcome text. After target-owner approval, send welcome explicitly under a new UUID; the old UUID replays its request-only receipt. Self-approval is refused, and blocked/block_all wins. Welcome uses ordinary message/FTS/recipient/dirty writes and one receipt in the same transaction.

Pre-commit failures roll back all state; post-commit lost responses replay the same UUID/intent. Receipt validation shares output schema, canonical preimage/hash and binding checks. Signal interruptions use existing S2b reconciliation. There is no reservation projection, activity GC or extra journal. Top-level status means readiness; signal.status means delivery health.

Rejection classification is assembled once: S2b first, only absent reasons from S2a, then the S2c phase delta. UUID conflicts, transport uncertainty and invalid output preserve pending. Read and management operations never mutate agent pending.

| Reason | Operation | Phase | existing_pending | Action |
| --- | --- | --- | --- | --- |
| LEASE_SELECTOR_CONFLICT | write | before_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTOR_CONFLICT | write | before_receipt_lookup | True | preserve_exact_bytes |
| LEASE_SELECTOR_EMPTY | write | before_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTOR_EMPTY | write | before_receipt_lookup | True | preserve_exact_bytes |
| ABSOLUTE_PATH_REQUIRED | write | before_receipt_lookup | False | clear_exact_own_planned |
| ABSOLUTE_PATH_REQUIRED | write | before_receipt_lookup | True | preserve_exact_bytes |
| ABSOLUTE_PATH_REQUIRED | read | read_only | False | unchanged |
| ABSOLUTE_PATH_REQUIRED | read | read_only | True | unchanged |
| TTL_OUT_OF_RANGE | write | before_receipt_lookup | False | clear_exact_own_planned |
| TTL_OUT_OF_RANGE | write | before_receipt_lookup | True | preserve_exact_bytes |
| WELCOME_INPUT_INCOMPLETE | write | before_receipt_lookup | False | clear_exact_own_planned |
| WELCOME_INPUT_INCOMPLETE | write | before_receipt_lookup | True | preserve_exact_bytes |
| RESERVATION_CONFLICT | write | after_receipt_lookup | False | clear_exact_own_planned |
| RESERVATION_CONFLICT | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_OWNER_REQUIRED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_OWNER_REQUIRED | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_NOT_FOUND | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_NOT_FOUND | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_EXPIRED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_EXPIRED | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_SELECTION_TOO_LARGE | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTION_TOO_LARGE | write | after_receipt_lookup | True | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | write | after_receipt_lookup | False | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | write | after_receipt_lookup | True | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | read | read_only | False | unchanged |
| ACTIVE_LEASE_RULES_UNKNOWN | read | read_only | True | unchanged |
| LEASE_ID_CAPACITY_REACHED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_ID_CAPACITY_REACHED | write | after_receipt_lookup | True | clear_exact_own_planned |
| CHECK_REQUIRES_CONCRETE_PATH | read | read_only | False | unchanged |
| CHECK_REQUIRES_CONCRETE_PATH | read | read_only | True | unchanged |
| STALE_LEASE_REVISION | management | management_only | either | unchanged |
| OWNER_REQUIRED | write | before_receipt_lookup | False | clear_exact_own_planned |
| OWNER_REQUIRED | write | before_receipt_lookup | True | preserve_exact_bytes |
| REQUEST_ID_CONFLICT | write | preserve_pending | False | preserve_exact_bytes |
| REQUEST_ID_CONFLICT | write | preserve_pending | True | preserve_exact_bytes |
| TRANSPORT_FAILED | write | preserve_pending | False | preserve_exact_bytes |
| TRANSPORT_FAILED | write | preserve_pending | True | preserve_exact_bytes |
| RESPONSE_INVALID | write | preserve_pending | False | preserve_exact_bytes |
| RESPONSE_INVALID | write | preserve_pending | True | preserve_exact_bytes |
| UNLISTED_SYNTHETIC_REASON | write | preserve_pending | False | preserve_exact_bytes |
| UNLISTED_SYNTHETIC_REASON | write | preserve_pending | True | preserve_exact_bytes |
| TIMESTAMP_INVALID | write | after_receipt_lookup | False | clear_exact_own_planned |
| TIMESTAMP_INVALID | write | after_receipt_lookup | True | clear_exact_own_planned |
| WRITER_FENCED | write | preserve_pending | False | preserve_exact_bytes |
| WRITER_FENCED | write | preserve_pending | True | preserve_exact_bytes |

### Expired history expectations

| Operation | Expired rows | ACTIVE | Selected | Result | Expired row mutation |
| --- | --- | --- | --- | --- | --- |
| release omitted | 10000 | 0 | 0 | ok | unchanged |
| release omitted | 10000 | 2 | 2 | ok | unchanged |
| renew omitted | 10000 | 0 | 0 | ok | unchanged |
| renew omitted | 10000 | 2 | 2 | ok | unchanged |
| retire hook | 10000 | 0 | 0 | ok | unchanged |
| retire hook | 10000 | 2 | 2 | ok | unchanged |
| fresh retired owner as_of | 10000 | 0 | 0 | ok | unchanged |
| fresh retired owner as_of | 10000 | 2 | 2 | ok | unchanged |
| release omitted | 10000 | 101 | 101 | LEASE_SELECTION_TOO_LARGE | unchanged |
| retire hook | 10000 | 101 | 101 | ok | unchanged |
| release explicit expired ID | 10000 | 0 | 1 | ok | only explicit ID changes |

## Fresh preparation and verification

Only a completed schema5 preparation receipt admits the config. Missing receipt rejects with CANDIDATE_SCHEMA_REQUIRES_REPREPARE before opening SQLite. Keep existing schema2/3/4 roots unchanged; prepare a fresh candidate from source in a different workspace/name. This also applies to development candidates made by unreleased master APIs.

Use the [S2a source/fence plan](global-server.en.md#s2a-prepare-a-fresh-schema3-candidate), with kind `orrery-s2c-preparation-plan-v1` and `agentstack-mail-global-prepare-s2c --plan plan.json`. Layout is fixed at `isolation_root/s2c-candidates/name/candidate/`. PR3/S2a/S2b preservation proofs precede schema5 construction in unpublished staging. Timestamp instants and IDs are preserved; choices.as_of controls retired ACTIVE-only conversion. reservation-proof.json records every row. The shared final manifest/gate → ready → rename → complete state machine resumes the same plan/UUID at every boundary and never overwrites foreign slots.

Run the completed config with a private HOME, unused port, passthrough and an absent env file, and wait for HTTP status=ok/profile=s2c-v1. Updating operator env/wrapper/hook/profile client_config to a new root is explicit work. Message/reservation/contact client wiring belongs to 4b/4c and activation to PR7.

```sh
fixture=$(mktemp -d)
HOME="$fixture" TMUX_TMPDIR="$fixture" .venv/bin/python -m pytest -q -o addopts= packages/agentstack_mail/tests/test_global_server_s2c.py
```

The suite covers real HTTP lifecycle/catalog, UUID interruptions, fresh publication checkpoints, 10000 expired rows and index SEARCH, force inspect recovery, macro atomicity, retired expired history, approval/block priority and receipt admission before SQLite open.

## Engine test migration

The approved inventory contains 72 named tests. Pure normalization, legacy app activity/liveness, Mail/delivery and default proxy assertions remain. Probe/collector-only assertions are removed; mixed cases retain their SQL/normalizer assertions. The existing 57-path performance gate measures the legacy app directly and remains unchanged. Explicit fixture transports use the real S2c runtime and hold no separate lease state.

| Source / test | Disposition |
| --- | --- |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_overlap_fixture | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_two_cwds_and_ignored_scope | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_symlink_dotdot_new_file_and_lifecycle | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_glob_symlink_directory_conflicts_for_future_file | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_glob_symlink_file_and_bounded_unknown | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_ambiguous_paths_rejected | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unicode_preserved_when_filesystem_distinguishes_it | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_linux_native_directory_flags_fail_closed | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_missing_unicode_leaves_follow_actual_filesystem | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unicode_glob_overlap_and_recent_activity | Retain overlap/unknown safety in SQL; remove activity/collector branch |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_distinguishing_unicode_rules_preserve_missing_names | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_unicode_rules_reject_acquire_and_prevent_stale | Retain overlap/unknown safety in SQL; remove activity/collector branch |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_unicode_directory_cannot_prove_empty_glob | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_supported_unicode_rules_match_canonical_variants | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_hfs_exclusions_and_unknown_apfs_unicode_version | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_case_alias_follows_filesystem | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_missing_nonascii_case_alias_never_grants_twice | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_nonascii_case_table_unknown_rejects_initial_acquire | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_nonascii_case_glob_activity_unknown_prevents_collection | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_case_sensitive_unicode_case_names_stay_distinct | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_future_file_under_case_alias_and_case_variant | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_explicit_wsl_mapping_and_virtual_type | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_shared_and_atomic_conflicts | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_owner_and_token_enforced | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_ttl_validation | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_no_early_release_but_ttl_and_owner_work | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_renew_extends_current_expiry_and_noop_is_not_activity | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_expired_gc_does_not_need_filesystem_evidence | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_raw_candidate_tool_names_share_lifecycle | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unmatched_initial_grace_then_stale | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_probe_permission_timeout_cap_and_unknown_anchor | Remove probe/collector branches; retain the independent dangling-anchor normalizer assertion |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_each_lease_actual_repo_git_activity_and_symlink | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_broad_glob_multiple_repos_and_git_failure | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_recent_deletion_commit_keeps_empty_scope_active | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_gc_rechecks_activity_after_probe | Remove obsolete probe/collector-only test |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_candidate_hook_any_folder_unmanaged_and_bound_outage | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_failed_candidate_edit_keeps_lease_without_dispatch | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_successful_candidate_edit_releases_lease | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_cli_and_bash32_hook_same_normalizer | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_candidate_not_published_or_activated | Retain default legacy/activation=false boundary |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_window_reconnect_preserves_id_token_and_distinguishes_resolved_uuid | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reply_permission_is_per_message_and_new_edge_is_direct | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_named_legacy_thread_and_conflicting_input_remain_rejected | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_retention_holds_ancestors_and_removes_complete_expired_sets | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reply_first_serializes_purge_in_the_same_write_transaction | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_read_ack_and_bcc_are_retained_without_cross_recipient_leak | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_persistent_reservations_preserve_imported_ids_and_owner_on_restart | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_persistent_lease_unknown_does_not_release_early_but_ttl_still_expires | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_terminal_observe_preserves_status_attempts_and_backoff | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_expired_lease_recovers_without_restarting_old_wake | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_same_message_id_is_independent_in_another_mail_instance | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_old_writer_generation_cannot_update_mail_leases_or_delivery | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_legacy_expiry_compares_instants_at_boundary | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_terminal_transition_is_enforced_by_database | Retain independent existing assertions |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reservation_latest_mail_is_selected_by_utc_instant | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_absolute_candidate_keeps_default_proxy_contract | Move lease assertions to S2c SQL/HTTP; retain independent Mail/delivery/proxy assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_bootstrap_binds_process_and_runtime_status_never_exposes_token | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_bridge_binding_wins_over_misleading_shell_identity | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_missing_bridge_binding_stops_without_guessing_a_name | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_subagent_bootstrap_waits_for_bridge_observed_pair_and_parent | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_subagent_bootstrap_rejects_unobserved_agent_id | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_allowlisted_tools_inject_bound_identity_and_owner_token | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_session_bound_surface_needs_no_caller_identity_after_bootstrap | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_unbound_status_fails_closed_without_identity_candidates | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_bridge_tools_reject_caller_supplied_identity_after_bootstrap | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_proxy_rejects_cross_binding_and_unsafe_reservation_paths | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_stdio_server_lists_only_allowlisted_tools_and_rejects_passthrough | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_operator_enrollment_is_absent_from_proxy_catalog_and_dispatch | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_only_bootstrap_accepts_caller_supplied_runtime_identity | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_mcp_server_script_runs_directly_without_plugin_root_env | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_whois_lets_a_child_confirm_a_recipient_name | Retain independent existing assertions |
| integrations/codex_app/tests/test_mcp_server.py / test_server_error_text_reaches_the_model_with_secrets_redacted | Retain independent existing assertions |
