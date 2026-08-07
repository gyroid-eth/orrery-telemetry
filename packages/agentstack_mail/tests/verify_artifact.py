"""Fail closed when a built distribution drops contract or license evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

DIVERGENCE_MANIFEST = "differential-expected-divergences-v2.json"
AUTHORIZATION_FIXTURE = "authorization-tools-v1.json"
EXPECTED_AUTHORIZATION_FIXTURE_SHA256 = (
    "c4da3e65c01daf727517490469ab02031764effd718e159cc30a8c2a1d0a0f61"
)

EXPECTED_BASELINES = {
    "live": {
        "python_namespace": "mcp_agent_mail",
        "source_head": "ad0e4788967d809979fa25004cf52545fdcd888a",
        "bundle_sha256": "55f03ea48a3279f090c4b93436af1d55f912c75c3f985e4ba06a8b95d39f7670",
        "tracked_patch_sha256": "8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac",
        "tool_fixture": "live-tools-list.json",
    },
    "core": {
        "python_namespace": "agentstack_mail",
        "approved_base": "de625ed1928fec533a85700b9f1b2892b5a61dc5",
        "contract_fixture": "compatibility-tools-v1.json",
    },
}

EXPECTED_DESCRIPTION_DIGESTS = {
    "request_contact": {
        "live": {
            "utf8_bytes": 791,
            "sha256": "b57583329712bdd01168c3c576957b049518c0270b0cc5730728eca3ead52b38",
        },
        "core": {
            "utf8_bytes": 736,
            "sha256": "25e18aad7eaa21282480539b393cb96abdd9f1a7a8ebb7fc6d0b266f114c5db4",
        },
    },
    "send_message": {
        "live": {
            "utf8_bytes": 4235,
            "sha256": "f1c3a155cf961241cd218796c5fcb14082bce82a418c8a0124599a8d7e59fe14",
        },
        "core": {
            "utf8_bytes": 4221,
            "sha256": "9f2e73cf925371ada7b6d168d8c4b8a7075edd2dca295b44a37f0e444c1f08c1",
        },
    },
    "whois": {
        "live": {
            "utf8_bytes": 742,
            "sha256": "139e1ee3071c20421acbda81699f182e6946663e7b61f856ae11bb04fba0ad2c",
        },
        "core": {
            "utf8_bytes": 687,
            "sha256": "1b1404e6ec24c23b390f37d5e243327df6f15769963a5c7d457aa87828468c65",
        },
    },
}

EXPECTED_STATIC_ALLOWLIST = {
    "isolation.namespace": {
        "category": "service_isolation",
        "selector": "service.namespace",
        "live": {
            "distribution": "mcp-agent-mail",
            "python_package": "mcp_agent_mail",
            "mcp_server_key": "mcp-agent-mail",
        },
        "core": {
            "distribution": "agentstack-mail",
            "python_package": "agentstack_mail",
            "mcp_server_key": "agentstack-mail",
        },
    },
    "isolation.environment": {
        "category": "service_isolation",
        "selector": "configuration.environment",
        "live": {"variable_prefix": "", "default_env_file": ".env"},
        "core": {
            "variable_prefix": "AGENTSTACK_MAIL_",
            "default_env_file": "~/.agentstack/mail/.env",
            "legacy_unprefixed_fallback": False,
        },
    },
    "isolation.default_paths": {
        "category": "service_isolation",
        "selector": "configuration.defaults",
        "live": {
            "port": 8765,
            "database": "./storage.sqlite3",
            "archive": "~/.mcp_agent_mail_git_mailbox_repo",
            "signals": "~/.mcp_agent_mail/signals",
        },
        "core": {
            "port": 18765,
            "database": "~/.agentstack/mail/storage.sqlite3",
            "archive": "~/.agentstack/mail/archive",
            "signals": "~/.agentstack/mail/signals",
        },
    },
    "isolation.provenance": {
        "category": "source_provenance",
        "selector": "source.baseline",
        "live": {
            "kind": "authenticated_git_bundle_plus_tracked_patch",
            "repository_audit_payload": True,
        },
        "core": {
            "kind": "derived_agentstack_distribution",
            "repository_audit_payload_in_distribution": False,
            "distribution_evidence": [
                "NOTICE.md",
                "AGENTSTACK_LICENSE",
                "UPSTREAM_LICENSE",
                "fixtures",
            ],
        },
    },
    "dependency.lazy_legacy_llm": {
        "category": "dependency_boundary",
        "selector": "runtime.llm_import",
        "live": {"legacy_llm_dependency": "runtime_baseline"},
        "core": {
            "legacy_llm_dependency": "optional_extra",
            "load_policy": "lazy_on_non_compatibility_llm_use",
        },
    },
}

EXPECTED_UNSELECTED_DECISIONS = {
    "D10": "concurrent reservation winner and SQLite lock semantics",
    "D11": "retire with active reservations or unread messages",
    "D12": "signal cleanup after crash, retirement, or stale consumer",
}

EXPECTED_SELECTED_DECISIONS = {
    "D1": {
        "id": "D1",
        "title": "conflicting token registration mutation",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "core_change",
        "cutover_state": "no_go",
        "resolution": "reject_explicit_conflicting_token_before_mutation",
        "scope": {
            "explicit_conflicting_token": "reject_without_durable_mutation",
            "same_token": "metadata_refresh_with_exactly_one_git_commit",
            "omitted_token": (
                "credential_retention_and_authority_semantics_unchanged_"
                "D6_selected_pre_existing_parity_D7_selected_not_implemented_no_go"
            ),
            "concurrent_conflicting_tokens_on_null_identity": (
                "single_atomic_writer_as_retained_unsafe_compatibility_not_claim_proof"
            ),
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d1.py::test_conflicting_explicit_token_is_rejected_without_durable_change",
            "tests/test_pending_decision_d1.py::test_same_explicit_token_updates_metadata_with_exactly_one_git_commit",
            "tests/test_pending_decision_d1.py::test_omitted_token_preserves_existing_credential_and_update_semantics",
            "tests/test_pending_decision_d1.py::test_concurrent_explicit_tokens_against_null_identity_are_first_winner",
        ],
    },
    "D2": {
        "id": "D2",
        "title": "expired contact link accepted",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "expiry_role": "stored_and_refreshed_not_an_authorization_boundary",
            "same_project_response_expiry_cases": ["past", "future", "null"],
            "expired_pending_accept": (
                "reuse_existing_row_approve_and_refresh_expiry"
            ),
            "local_send_expiry_cases": ["past", "future", "null"],
            "explicit_cross_project_send_expiry_cases": [
                "past",
                "future",
                "null",
            ],
            "explicit_cross_project_reply_expiry_cases": [
                "past",
                "future",
                "null",
            ],
            "pending_status_controls": (
                "reject_without_new_message_recipient_archive_signal_or_git_state"
            ),
            "local_reply": "not_claimed_because_it_does_not_query_agent_links",
            "not_claimed": [
                "accept_false",
                "cross_project_response",
                "concurrency",
                "future_strict_expiry_denial",
                "auto_handshake_enabled",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_upstream_parity_d2.py::test_d2_expiry_semantics_match_frozen_live_without_core_change"
        ],
    },
    "D3": {
        "id": "D3",
        "title": "cross-project intro/reply identity",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "foreign_source_row_intro": (
                "target_project_message_uses_source_project_sender_row"
            ),
            "immediate_target_reply": (
                "foreign_sender_id_unresolvable_in_target_project"
            ),
            "approved_explicit_send": (
                "creates_target_local_same_name_null_token_alias_with_copied_"
                "metadata_and_uses_it_as_sender"
            ),
            "reply_after_fresh_process": (
                "targets_alias_in_target_project_preserves_thread_and_does_not_"
                "reach_source_inbox"
            ),
            "not_claimed": [
                "contact_expiry_or_no_pending_response",
                "invalid_policy_or_sender_token_omission",
                "pre_existing_same_name_target_agent_or_alias_reuse",
                "migration_reject_mode_concurrency_or_crash",
                "ownership_or_safe_enrollment_of_null_alias",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d3.py::test_d3_selected_upstream_parity_requirement"
        ],
    },
    "D4": {
        "id": "D4",
        "title": "accept response without pending",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "measured_path": (
                "same_project_respond_contact_accept_true_without_existing_link"
            ),
            "result": "approved_true_updated_one_returned_expiry_equals_persisted",
            "created_link": (
                "one_directed_approved_empty_reason_created_equals_updated_"
                "expiry_plus_requested_600_seconds"
            ),
            "observed_absences": (
                "no_new_message_or_recipient_and_no_observed_archive_git_or_"
                "signal_change"
            ),
            "not_claimed": [
                "accept_false_or_cross_project",
                "existing_link_states_or_replay",
                "concurrency_authorization_or_strict_no_pending",
                "other_ttl_cases_or_downstream_delivery",
                "unobserved_database_columns",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d4.py::test_d4_selected_parity_accept_without_pending_creates_approved_link"
        ],
    },
    "D5": {
        "id": "D5",
        "title": "invalid contact policy coerced auto",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "measured_path": "same_project_existing_agent_starting_contacts_only",
            "invalid_inputs": ["not-a-policy", ""],
            "selected_behavior": (
                "succeed_return_auto_and_persist_observed_contact_policy_only_change"
            ),
            "diagnostic_only": (
                "recent_contact_and_followup_message_sequence_not_selected_or_compared"
            ),
            "not_claimed": [
                "valid_mixed_case_whitespace_or_other_invalid_shapes",
                "missing_agent_or_project",
                "warnings_audit_or_strict_transitional_modes",
                "messaging_contact_link_or_sender_auth_semantics",
                "concurrency",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d5.py::test_d5_selected_parity_invalid_and_empty_policy_coerce_to_auto"
        ],
    },
    "D6": {
        "id": "D6",
        "title": "missing sender token succeeds",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "measured_path": "same_project_send_message_to_open_recipient",
            "sender_cohort": (
                "explicitly_registered_non_null_caller_supplied_token"
            ),
            "missing_sender_token": (
                "succeeds_verified_sender_false_and_commits_one_delivery"
            ),
            "wrong_sender_token_control": (
                "rejects_with_observed_durable_projection_unchanged"
            ),
            "credential_non_disclosure": (
                "complete_raw_mcp_results_plus_fixture_transcript_and_output"
            ),
            "not_claimed": [
                "correct_token_or_generated_unavailable_token",
                "null_macro_migrated_or_grandfathered_identity",
                "non_open_contact_policy",
                "cross_project_or_other_send_entrypoints",
                "concurrency_rotation_recovery_claim_or_strict_enforcement",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d6.py::test_d6_frozen_live_and_core_match_missing_sender_token_behavior"
        ],
    },
    "D7": {
        "id": "D7",
        "title": "owner tools name-only auth",
        "decision_state": "selected",
        "implementation_state": "not_implemented",
        "cutover_state": "no_go",
        "resolution": "limit_name_only_retire_to_idempotent_retired_null_legacy",
        "scope": {
            "name_only_soft_retire": (
                "only_idempotent_reretire_of_already_retired_null_token_legacy_"
                "row_preserving_retired_at_with_zero_durable_mutation"
            ),
            "active_null_token_name_only_retire": "deny",
            "stronger_owner_operations": (
                "unretire_hard_delete_transfer_and_project_wide_require_future_"
                "principal_or_administrator"
            ),
            "enforcement_prerequisite": (
                "stop_all_new_null_token_creation_paths_before_enforcement_"
                "ordering_only_not_current_behavior"
            ),
            "principal_admin_mechanism": "unselected",
            "lifecycle_disposition": "unchanged_D11_unselected",
        },
        "rationale": [
            "name_only_retire_is_timing_selectable_receive_denial",
            "unretire_cannot_restore_rejected_sends",
            "observed_zero_rows_is_machine_fact_not_product_fact",
        ],
        "allowlisted": False,
        "comparator_disposition": "fail",
    },
    "D8": {
        "id": "D8",
        "title": "DB persists after archive failure",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "measured_path": "same_project_send_message_to_open_recipient",
            "sender_cohort": (
                "explicitly_registered_non_null_caller_supplied_token"
            ),
            "literal_sigkill_seams": [
                "after_canonical_bundle_write_before_outbox_write",
                "after_outbox_bundle_write_before_inbox_write",
                (
                    "after_canonical_outbox_inbox_writes_and_index_stage_"
                    "before_git_commit"
                ),
            ],
            "database_survival": (
                "one_selected_subject_message_to_recipient_relationship_"
                "survives_each_sigkill"
            ),
            "archive_git_projection": {
                "after_canonical_write": (
                    "canonical_only_head_unchanged_staging_empty_no_message_commit"
                ),
                "after_outbox_write": (
                    "canonical_and_sender_outbox_only_head_unchanged_staging_"
                    "empty_no_message_commit"
                ),
                "after_three_copies_staged": (
                    "canonical_sender_outbox_recipient_inbox_exist_and_are_"
                    "staged_head_unchanged_no_message_commit"
                ),
            },
            "message_bundle_exception": {
                "tool_failure": (
                    "mcp_is_error_true_and_injected_exception_marker_present"
                ),
                "database": (
                    "one_selected_subject_message_to_recipient_relationship_"
                    "persists"
                ),
                "archive": "subject_absent",
            },
            "not_claimed": [
                "ordinary_failures_outside_write_message_bundle",
                "other_archive_exceptions_or_additive_tool_error_fields",
                "registration_or_profile_write_failure",
                "instruction_level_failure_inside_native_git_commit",
                "restart_retry_reconciliation_or_power_loss",
                "multi_recipient_attachments_or_threaded_message",
                "concurrency",
                "signal_lifecycle_fetch_cleanup_or_D12",
                "unselected_database_fields",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d8_d9.py::test_d8_selected_parity_database_and_staged_bundle_after_precommit_sigkill",
            "tests/test_pending_decision_d8_d9.py::test_d8_selected_parity_completed_bundle_subset_after_write_sigkill",
            "tests/test_pending_decision_d8_d9.py::test_d8_selected_parity_message_bundle_exception_leaves_committed_database_without_archive",
        ],
    },
    "D9": {
        "id": "D9",
        "title": "read/ack partial commit",
        "decision_state": "selected",
        "implementation_state": "implemented",
        "implementation_origin": "pre_existing_parity",
        "cutover_state": "no_go",
        "resolution": "match_frozen_live",
        "scope": {
            "measured_path": (
                "same_project_acknowledge_message_for_one_ack_required_"
                "direct_to_recipient"
            ),
            "initial_recipient_state": (
                "selected_receipt_read_ts_null_ack_ts_null"
            ),
            "literal_sigkill_seam": (
                "after_committed_read_helper_returns_before_ack_helper_call"
            ),
            "ordinary_exception_seam": "at_ack_helper_entry_after_read_commit",
            "durable_recipient_state": (
                "read_ts_present_ack_ts_absent_after_each_selected_seam"
            ),
            "not_claimed": [
                (
                    "exact_timestamp_ids_names_recipient_kind_helper_return_"
                    "or_error_envelope"
                ),
                "missing_recipient_normal_success_or_replay",
                "other_exception_or_process_crash_windows",
                "restart_retry_idempotency_reconciliation_or_migration",
                "concurrency_or_races",
                "archive_git_or_D8",
                "signal_lifecycle_fetch_cleanup_or_D12",
                "other_message_recipient_or_database_fields",
            ],
        },
        "allowlisted": False,
        "comparator_disposition": "assert_selected_behavior",
        "verification": [
            "tests/test_pending_decision_d8_d9.py::test_d9_selected_parity_read_commits_before_ack_after_between_commit_sigkill",
            "tests/test_pending_decision_d8_d9.py::test_d9_selected_parity_ack_helper_exception_preserves_read_without_ack",
        ],
    },
}

EXPECTED_DECISION_IDS = {f"D{index}" for index in range(1, 13)}

EXPECTED_LIVE_RESOURCE_TEMPLATE_URIS = [
    "resource://agents/{project_key}{?format}",
    "resource://config/environment{?format}",
    "resource://file_reservations/{slug}{?active_only,format}",
    "resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit,format}",
    "resource://mailbox-with-commits/{agent}{?project,limit,format}",
    "resource://mailbox/{agent}{?project,limit,format}",
    "resource://message/{message_id}{?project,format}",
    "resource://outbox/{agent}{?project,limit,include_bodies,since_ts,format}",
    "resource://project/{slug}{?format}",
    "resource://projects{?format}",
    "resource://thread/{thread_id}{?project,include_bodies,format}",
    "resource://tooling/capabilities/{agent}{?project,format}",
    "resource://tooling/directory{?format}",
    "resource://tooling/locks{?format}",
    "resource://tooling/metrics{?format}",
    "resource://tooling/recent/{window_seconds}{?agent,project,format}",
    "resource://tooling/schemas{?format}",
    "resource://views/ack-overdue/{agent}{?project,ttl_minutes,limit,format}",
    "resource://views/ack-required/{agent}{?project,limit,format}",
    "resource://views/acks-stale/{agent}{?project,ttl_seconds,limit,format}",
    "resource://views/urgent-unread/{agent}{?project,limit,format}",
]

EXPECTED_NORMALIZATION_BLIND_SPOTS = [
    {
        "id": "rich_tool_call_timing_presentation",
        "scope": "durable Git log Rich tool-call panels only",
        "ignored": [
            "measured duration in milliseconds",
            "duration-derived speed icon and completion footer",
        ],
        "consequence": (
            "This behavior differential cannot detect live/Core performance-class "
            "regressions; performance must be measured by a separate gate."
        ),
    }
]

REQUIRED_RUNTIME_MODULES = {
    "__init__.py",
    "app.py",
    "authorization.py",
    "boundary.py",
    "config.py",
    "contract.py",
    "db.py",
    "guard.py",
    "llm.py",
    "model_normalize.py",
    "models.py",
    "rich_logger.py",
    "storage.py",
    "utils.py",
}

WHEEL_REQUIRED_SUFFIXES = {
    ".dist-info/licenses/AGENTSTACK_LICENSE",
    ".dist-info/licenses/UPSTREAM_LICENSE",
    "agentstack_mail/NOTICE.md",
    f"agentstack_mail/fixtures/{AUTHORIZATION_FIXTURE}",
    "agentstack_mail/fixtures/compatibility-tools-v1.json",
    f"agentstack_mail/fixtures/{DIVERGENCE_MANIFEST}",
    "agentstack_mail/fixtures/live-tools-list.json",
} | {f"agentstack_mail/{module}" for module in REQUIRED_RUNTIME_MODULES}

SDIST_REQUIRED_SUFFIXES = {
    "/AGENTSTACK_LICENSE",
    "/UPSTREAM_LICENSE",
    "/NOTICE.md",
    "/README.md",
    f"/fixtures/{AUTHORIZATION_FIXTURE}",
    "/fixtures/compatibility-tools-v1.json",
    f"/fixtures/{DIVERGENCE_MANIFEST}",
    "/fixtures/live-tools-list.json",
    "/pyproject.toml",
    "/tests/test_decision_manifest.py",
    "/tests/test_pending_decision_d1.py",
    "/tests/test_pending_decision_d3.py",
    "/tests/test_pending_decision_d4.py",
    "/tests/test_pending_decision_d5.py",
    "/tests/test_pending_decision_d6.py",
    "/tests/test_pending_decision_d8_d9.py",
    "/tests/test_upstream_parity_d2.py",
    "/tests/verify_installed_contract.py",
    "/tests/verify_artifact.py",
} | {f"/src/agentstack_mail/{module}" for module in REQUIRED_RUNTIME_MODULES}

REQUIRED_METADATA = {
    "Name: agentstack-mail",
    "License-Expression: LicenseRef-PolyForm-Perimeter-1.0.1 AND LicenseRef-MCP-Agent-Mail",
    "Requires-Dist: fastmcp==2.13.0.2",
    "Requires-Dist: pydantic==2.12.5",
}


def _missing_suffixes(names: set[str], required: set[str]) -> list[str]:
    return sorted(
        suffix
        for suffix in required
        if not any(name.endswith(suffix) for name in names)
    )


def _old_namespace_imports(files: dict[str, bytes]) -> list[str]:
    old_namespace_imports: list[str] = []
    for name, content in sorted(files.items()):
        tree = ast.parse(content, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            if any(
                module == "mcp_agent_mail" or module.startswith("mcp_agent_mail.")
                for module in modules
            ):
                old_namespace_imports.append(name)
    return old_namespace_imports


def _assert_safe_paths(names: list[str], *, artifact: str) -> None:
    if len(names) != len(set(names)):
        raise SystemExit(f"{artifact} contains duplicate member paths")
    unsafe = [
        name
        for name in names
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
    ]
    if unsafe:
        raise SystemExit(
            f"{artifact} contains unsafe member paths: {', '.join(sorted(unsafe))}"
        )


def _assert_exact_runtime_modules(
    names: set[str],
    *,
    marker: str,
    artifact: str,
) -> None:
    actual = {
        name.split(marker, 1)[1]
        for name in names
        if marker in name and name.endswith(".py")
    }
    if actual != REQUIRED_RUNTIME_MODULES:
        raise SystemExit(
            f"{artifact} runtime module mismatch: "
            f"missing={sorted(REQUIRED_RUNTIME_MODULES - actual)}, "
            f"extra={sorted(actual - REQUIRED_RUNTIME_MODULES)}"
        )


def _assert_metadata(content: bytes, *, artifact: str) -> None:
    text = content.decode("utf-8")
    missing = sorted(fragment for fragment in REQUIRED_METADATA if fragment not in text)
    if missing:
        raise SystemExit(
            f"{artifact} metadata is missing required fields: {', '.join(missing)}"
        )


def _content_with_suffix(
    files: dict[str, bytes],
    suffix: str,
    *,
    artifact: str,
) -> bytes:
    matches = [content for name, content in files.items() if name.endswith(suffix)]
    if len(matches) != 1:
        raise SystemExit(
            f"{artifact} must contain exactly one member ending with {suffix!r}"
        )
    return matches[0]


def _json_object(content: bytes, *, label: str, artifact: str) -> dict[str, object]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{artifact} contains invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{artifact} {label} must contain a JSON object")
    return value


def _digest_record(value: str) -> dict[str, object]:
    content = value.encode("utf-8")
    return {
        "utf8_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _assert_authorization_fixture(
    content: bytes,
    compatibility_content: bytes,
    *,
    artifact: str,
) -> None:
    digest = hashlib.sha256(content).hexdigest()
    if digest != EXPECTED_AUTHORIZATION_FIXTURE_SHA256:
        raise SystemExit(
            f"{artifact} authorization fixture digest changed: "
            f"expected={EXPECTED_AUTHORIZATION_FIXTURE_SHA256}, actual={digest}"
        )
    fixture = _json_object(
        content,
        label=AUTHORIZATION_FIXTURE,
        artifact=artifact,
    )
    compatibility = _json_object(
        compatibility_content,
        label="compatibility-tools-v1.json",
        artifact=artifact,
    )
    expected_names = set(compatibility.get("compatibility_union", []))
    tools = fixture.get("tools")
    if not isinstance(tools, dict) or set(tools) != expected_names:
        raise SystemExit(
            f"{artifact} authorization fixture must cover exact tool union"
        )
    if fixture.get("catalog_version") != 1:
        raise SystemExit(f"{artifact} authorization catalog version changed")
    if fixture.get("default_principal_candidate") != "local-single-principal":
        raise SystemExit(f"{artifact} authorization default principal changed")
    if fixture.get("rule_status") != (
        "prospective_non_binding_except_selected_D7_retire_boundary"
    ):
        raise SystemExit(f"{artifact} authorization rule status changed")
    if fixture.get("default_policy") != {
        "decision": "would_allow",
        "reason": "policy_empty_default_allow",
    }:
        raise SystemExit(f"{artifact} authorization default policy changed")


def _core_tool_descriptions(
    app_source: bytes,
    tool_names: set[str],
    *,
    artifact: str,
) -> dict[str, str]:
    try:
        tree = ast.parse(app_source, filename="agentstack_mail/app.py")
    except (SyntaxError, ValueError) as exc:
        raise SystemExit(f"{artifact} app.py cannot be parsed: {exc}") from exc
    matches: dict[str, list[str]] = {name: [] for name in tool_names}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in matches:
            continue
        description = ast.get_docstring(node, clean=True)
        if description is not None:
            matches[node.name].append(description)
    invalid = sorted(name for name, values in matches.items() if len(values) != 1)
    if invalid:
        raise SystemExit(
            f"{artifact} must contain exactly one documented tool body for: "
            + ", ".join(invalid)
        )
    return {name: values[0] for name, values in matches.items()}


def _assert_expected_divergences_manifest(
    manifest_content: bytes,
    compatibility_content: bytes,
    live_tools_content: bytes,
    app_source: bytes,
    *,
    artifact: str,
) -> None:
    manifest = _json_object(
        manifest_content,
        label=DIVERGENCE_MANIFEST,
        artifact=artifact,
    )
    expected_top_level = {
        "manifest_version",
        "contract_version",
        "comparison_policy",
        "baselines",
        "intentional_differences",
        "product_decisions",
    }
    if set(manifest) != expected_top_level:
        raise SystemExit(
            f"{artifact} divergence manifest top-level keys do not match v2"
        )
    if manifest["manifest_version"] != 2 or manifest["contract_version"] != 1:
        raise SystemExit(
            f"{artifact} divergence manifest must be schema version 2, contract 1"
        )
    expected_policy = {
        "default": "fail_on_difference",
        "allowlist": "intentional_differences.allowlisted_entries_only",
        "unselected_product_decisions": "fail_on_observation",
        "selected_implemented_product_decisions": "assert_selected_behavior",
        "selected_not_implemented_product_decisions": "fail_on_observation",
    }
    if manifest["comparison_policy"] != expected_policy:
        raise SystemExit(f"{artifact} divergence manifest is not fail-closed")
    if manifest["baselines"] != EXPECTED_BASELINES:
        raise SystemExit(f"{artifact} divergence manifest baselines changed")

    intentional = manifest["intentional_differences"]
    if not isinstance(intentional, dict) or set(intentional) != {
        "server_topology",
        "normalization_blind_spots",
        "allowlisted_entries",
    }:
        raise SystemExit(
            f"{artifact} divergence manifest intentional-differences shape is invalid"
        )
    if intentional["normalization_blind_spots"] != EXPECTED_NORMALIZATION_BLIND_SPOTS:
        raise SystemExit(f"{artifact} normalization blind spots changed")
    entries = intentional["allowlisted_entries"]
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise SystemExit(f"{artifact} divergence manifest allowlist must be a list")
    entries_by_id = {item.get("id"): item for item in entries}
    if len(entries_by_id) != len(entries):
        raise SystemExit(f"{artifact} divergence manifest has duplicate allowlist ids")

    decisions = manifest["product_decisions"]
    if not isinstance(decisions, list) or not all(
        isinstance(item, dict) for item in decisions
    ):
        raise SystemExit(f"{artifact} product decisions must be a list")
    decisions_by_id = {item.get("id"): item for item in decisions}
    if len(decisions_by_id) != len(decisions):
        raise SystemExit(f"{artifact} product decisions contain duplicate ids")

    decision_ids = set(decisions_by_id)
    if decision_ids != EXPECTED_DECISION_IDS:
        missing = sorted(EXPECTED_DECISION_IDS - decision_ids)
        extra = sorted(decision_ids - EXPECTED_DECISION_IDS)
        raise SystemExit(
            f"{artifact} product decision ledger ids changed: "
            f"missing={missing}, extra={extra}"
        )
    decision_allowlisted = sorted(decision_ids & set(entries_by_id))
    if decision_allowlisted:
        raise SystemExit(
            f"{artifact} product decisions must not be allowlisted: "
            f"{decision_allowlisted}"
        )

    implementation_origins = {"pre_existing_parity", "core_change"}
    for decision_id, decision in decisions_by_id.items():
        if decision.get("implementation_state") == "implemented":
            if decision.get("implementation_origin") not in implementation_origins:
                raise SystemExit(
                    f"{artifact} implemented decision {decision_id} has invalid or missing origin"
                )
        elif "implementation_origin" in decision:
            raise SystemExit(
                f"{artifact} non-implemented decision {decision_id} must not have origin"
            )

    for decision_id, title in EXPECTED_UNSELECTED_DECISIONS.items():
        if decisions_by_id[decision_id] != {
            "id": decision_id,
            "title": title,
            "decision_state": "unselected",
            "implementation_state": "not_implemented",
            "cutover_state": "no_go",
            "allowlisted": False,
            "comparator_disposition": "fail",
        }:
            raise SystemExit(f"{artifact} unselected decision {decision_id} changed")

    for decision_id, expected in EXPECTED_SELECTED_DECISIONS.items():
        if decisions_by_id[decision_id] != expected:
            raise SystemExit(
                f"{artifact} selected product decision {decision_id} changed"
            )

    expected_allowed_ids = {
        *(f"description.{name}" for name in EXPECTED_DESCRIPTION_DIGESTS),
        "topology.publication_surface",
        *EXPECTED_STATIC_ALLOWLIST,
    }
    if set(entries_by_id) != expected_allowed_ids:
        raise SystemExit(f"{artifact} divergence manifest allowlist ids changed")
    for entry in entries:
        if set(entry) != {
            "id",
            "category",
            "selector",
            "comparator_disposition",
            "live",
            "core",
            "reason",
        }:
            raise SystemExit(
                f"{artifact} divergence allowlist entry {entry.get('id')!r} has invalid keys"
            )
        if entry["comparator_disposition"] != "allow":
            raise SystemExit(
                f"{artifact} divergence allowlist entry {entry['id']!r} is not allowed"
            )
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise SystemExit(
                f"{artifact} divergence allowlist entry {entry['id']!r} lacks a reason"
            )

    compatibility = _json_object(
        compatibility_content,
        label="compatibility-tools-v1.json",
        artifact=artifact,
    )
    live_tools = _json_object(
        live_tools_content,
        label="live-tools-list.json",
        artifact=artifact,
    )
    compatibility_names = compatibility.get("compatibility_union")
    live_tool_records = live_tools.get("tools")
    if (
        compatibility.get("contract_version") != 1
        or not isinstance(compatibility_names, list)
        or not all(isinstance(name, str) for name in compatibility_names)
    ):
        raise SystemExit(f"{artifact} compatibility fixture has invalid tool names")
    if not isinstance(live_tool_records, list) or not all(
        isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and isinstance(tool.get("description", ""), str)
        for tool in live_tool_records
    ):
        raise SystemExit(f"{artifact} live tool fixture has invalid records")
    if len(set(compatibility_names)) != len(compatibility_names):
        raise SystemExit(f"{artifact} compatibility fixture has duplicate tools")
    live_by_name = {tool["name"]: tool for tool in live_tool_records}
    if len(live_by_name) != len(live_tool_records):
        raise SystemExit(f"{artifact} live tool fixture has duplicate tools")

    topology_entry = entries_by_id["topology.publication_surface"]
    expected_live_topology = {
        "tool_count": 40,
        "resource_count": 0,
        "resource_names": [],
        "resource_template_count": 21,
        "resource_template_uris": EXPECTED_LIVE_RESOURCE_TEMPLATE_URIS,
        "prompt_count": 0,
        "prompt_names": [],
        "tool_names": sorted(live_by_name),
    }
    expected_core_topology = {
        "tool_count": 22,
        "resource_count": 0,
        "resource_names": [],
        "resource_template_count": 0,
        "resource_template_uris": [],
        "prompt_count": 0,
        "prompt_names": [],
        "tool_names": sorted(compatibility_names),
    }
    if (
        topology_entry["category"] != "server_topology"
        or topology_entry["selector"] != "server"
    ):
        raise SystemExit(f"{artifact} topology allowance selector changed")
    if topology_entry["live"] != expected_live_topology:
        raise SystemExit(f"{artifact} live topology allowance changed")
    if topology_entry["core"] != expected_core_topology:
        raise SystemExit(f"{artifact} core topology allowance changed")
    expected_topology_summary = {
        "live": {
            "tool_count": 40,
            "resource_count": 0,
            "resource_template_count": 21,
            "prompt_count": 0,
        },
        "core": {
            "tool_count": 22,
            "resource_count": 0,
            "resource_template_count": 0,
            "prompt_count": 0,
        },
    }
    if intentional["server_topology"] != expected_topology_summary:
        raise SystemExit(f"{artifact} topology summary changed")

    description_names = set(EXPECTED_DESCRIPTION_DIGESTS)
    core_descriptions = _core_tool_descriptions(
        app_source,
        description_names,
        artifact=artifact,
    )
    for tool_name, expected_digests in EXPECTED_DESCRIPTION_DIGESTS.items():
        entry = entries_by_id[f"description.{tool_name}"]
        if entry["category"] != "tool_description" or entry["selector"] != (
            f"tools.{tool_name}.description"
        ):
            raise SystemExit(
                f"{artifact} description allowance selector changed for {tool_name}"
            )
        if (
            entry["live"] != expected_digests["live"]
            or entry["core"] != expected_digests["core"]
        ):
            raise SystemExit(
                f"{artifact} description allowance digest changed for {tool_name}"
            )
        if (
            _digest_record(live_by_name[tool_name].get("description", ""))
            != entry["live"]
        ):
            raise SystemExit(
                f"{artifact} live fixture no longer matches the {tool_name} allowance"
            )
        if _digest_record(core_descriptions[tool_name]) != entry["core"]:
            raise SystemExit(
                f"{artifact} app.py no longer matches the {tool_name} allowance"
            )

    for entry_id, expected in EXPECTED_STATIC_ALLOWLIST.items():
        entry = entries_by_id[entry_id]
        for key in ("category", "selector", "live", "core"):
            if entry[key] != expected[key]:
                raise SystemExit(
                    f"{artifact} static divergence allowance changed for {entry_id}"
                )


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        member_names = archive.namelist()
        _assert_safe_paths(member_names, artifact="wheel")
        names = set(member_names)
        files = {name: archive.read(name) for name in names if not name.endswith("/")}
        python_files = {
            name: content
            for name, content in files.items()
            if name.startswith("agentstack_mail/") and name.endswith(".py")
        }
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise SystemExit("wheel must contain exactly one .dist-info/METADATA")
        metadata = files[metadata_names[0]]

    missing = _missing_suffixes(names, WHEEL_REQUIRED_SUFFIXES)
    if missing:
        raise SystemExit(f"wheel is missing required artifacts: {', '.join(missing)}")
    if any(name.startswith("agentstack_mail/provenance/") for name in names):
        raise SystemExit("wheel must not contain the repository-only provenance bundle")
    _assert_exact_runtime_modules(
        names,
        marker="agentstack_mail/",
        artifact="wheel",
    )
    _assert_metadata(metadata, artifact="wheel")
    _assert_expected_divergences_manifest(
        _content_with_suffix(
            files,
            f"agentstack_mail/fixtures/{DIVERGENCE_MANIFEST}",
            artifact="wheel",
        ),
        _content_with_suffix(
            files,
            "agentstack_mail/fixtures/compatibility-tools-v1.json",
            artifact="wheel",
        ),
        _content_with_suffix(
            files,
            "agentstack_mail/fixtures/live-tools-list.json",
            artifact="wheel",
        ),
        _content_with_suffix(files, "agentstack_mail/app.py", artifact="wheel"),
        artifact="wheel",
    )
    _assert_authorization_fixture(
        _content_with_suffix(
            files,
            f"agentstack_mail/fixtures/{AUTHORIZATION_FIXTURE}",
            artifact="wheel",
        ),
        _content_with_suffix(
            files,
            "agentstack_mail/fixtures/compatibility-tools-v1.json",
            artifact="wheel",
        ),
        artifact="wheel",
    )
    old_namespace_imports = _old_namespace_imports(python_files)
    if old_namespace_imports:
        raise SystemExit(
            "wheel contains imports from the old namespace: "
            + ", ".join(sorted(set(old_namespace_imports)))
        )


def verify_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        all_members = archive.getmembers()
        member_names = [member.name for member in all_members]
        _assert_safe_paths(member_names, artifact="sdist")
        unsafe_types = [
            member.name
            for member in all_members
            if member.issym() or member.islnk() or member.isdev()
        ]
        if unsafe_types:
            raise SystemExit(
                "sdist contains link or device members: "
                + ", ".join(sorted(unsafe_types))
            )
        top_levels = {PurePosixPath(name).parts[0] for name in member_names if name}
        if len(top_levels) != 1:
            raise SystemExit("sdist must contain exactly one top-level directory")
        members = [member for member in archive.getmembers() if member.isfile()]
        names = {member.name for member in members}
        files: dict[str, bytes] = {}
        for member in members:
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SystemExit(f"sdist member is not readable: {member.name}")
            files[member.name] = extracted.read()
        python_files = {
            name: content
            for name, content in files.items()
            if "/src/agentstack_mail/" in name and name.endswith(".py")
        }
        metadata_names = [name for name in names if name.endswith("/PKG-INFO")]
        if len(metadata_names) != 1:
            raise SystemExit("sdist must contain exactly one PKG-INFO")
        metadata = files[metadata_names[0]]

    missing = _missing_suffixes(names, SDIST_REQUIRED_SUFFIXES)
    if missing:
        raise SystemExit(f"sdist is missing required artifacts: {', '.join(missing)}")
    if any("/provenance/" in name for name in names):
        raise SystemExit("sdist must not contain repository-only provenance artifacts")
    _assert_exact_runtime_modules(
        names,
        marker="/src/agentstack_mail/",
        artifact="sdist",
    )
    _assert_metadata(metadata, artifact="sdist")
    _assert_expected_divergences_manifest(
        _content_with_suffix(
            files,
            f"/fixtures/{DIVERGENCE_MANIFEST}",
            artifact="sdist",
        ),
        _content_with_suffix(
            files,
            "/fixtures/compatibility-tools-v1.json",
            artifact="sdist",
        ),
        _content_with_suffix(
            files,
            "/fixtures/live-tools-list.json",
            artifact="sdist",
        ),
        _content_with_suffix(
            files,
            "/src/agentstack_mail/app.py",
            artifact="sdist",
        ),
        artifact="sdist",
    )
    _assert_authorization_fixture(
        _content_with_suffix(
            files,
            f"/fixtures/{AUTHORIZATION_FIXTURE}",
            artifact="sdist",
        ),
        _content_with_suffix(
            files,
            "/fixtures/compatibility-tools-v1.json",
            artifact="sdist",
        ),
        artifact="sdist",
    )
    old_namespace_imports = _old_namespace_imports(python_files)
    if old_namespace_imports:
        raise SystemExit(
            "sdist contains imports from the old namespace: "
            + ", ".join(sorted(set(old_namespace_imports)))
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    if args.artifact.suffix == ".whl":
        verify_wheel(args.artifact)
    elif args.artifact.name.endswith(".tar.gz"):
        verify_sdist(args.artifact)
    else:
        raise SystemExit(f"unsupported distribution artifact: {args.artifact}")


if __name__ == "__main__":
    main()
