from __future__ import annotations

import json
import os

import pytest

from agentstack_codex_app.identity_store import (
    IdentityStore,
    IdentityStoreError,
    build_binding,
    external_id_for,
)


def _mode(path) -> int:
    return os.stat(path).st_mode & 0o777


def test_binding_and_owner_token_are_separate_private_files(tmp_path):
    store = IdentityStore(tmp_path / "identity")
    binding = build_binding(
        session_id="session-example",
        agent_id=None,
        agent_name="Calm-Noether",
        project_key="/workspace/example",
        now="2026-01-01T00:00:00Z",
    )
    store.save(binding)
    store.store_owner_token(binding["external_id"], "owner-secret")

    assert store.resolve(binding["external_id"]) == binding
    assert store.load_owner_token(binding["external_id"]) == "owner-secret"
    binding_file = next(store.bindings_dir.glob("*.json"))
    secret_file = next(store.secrets_dir.glob("*.token"))
    assert "owner-secret" not in binding_file.read_text(encoding="utf-8")
    assert _mode(store.root) == 0o700
    assert _mode(store.bindings_dir) == 0o700
    assert _mode(store.secrets_dir) == 0o700
    assert _mode(binding_file) == 0o600
    assert _mode(secret_file) == 0o600


def test_subagent_binding_has_canonical_parent():
    binding = build_binding(
        session_id="session-example",
        agent_id="child-example",
        agent_name="Quiet-Curie",
        project_key="/workspace/example",
    )
    assert binding["external_id"] == "codex:session-example:sub:child-example"
    assert binding["parent_external_id"] == external_id_for("session-example")


def test_existing_external_id_cannot_be_reassigned(tmp_path):
    store = IdentityStore(tmp_path)
    binding = build_binding(
        session_id="session-example",
        agent_id=None,
        agent_name="Calm-Noether",
        project_key="/workspace/example",
    )
    store.save(binding)
    hijack = dict(binding, agent_name="Quiet-Curie")
    with pytest.raises(IdentityStoreError, match="immutable"):
        store.save(hijack)


def test_server_confirmed_name_can_be_reconciled_without_general_reassignment(
    tmp_path,
):
    store = IdentityStore(tmp_path)
    binding = store.save(
        build_binding(
            session_id="session-example",
            agent_id=None,
            agent_name="Wild-McClintock",
            project_key="/workspace/example",
        )
    )

    reconciled = store.reconcile_agent_name(
        binding["external_id"],
        "WildMcClintock",
    )

    assert reconciled["agent_name"] == "WildMcClintock"
    assert reconciled["external_id"] == binding["external_id"]
    assert reconciled["created_at"] == binding["created_at"]
    with pytest.raises(IdentityStoreError, match="immutable"):
        store.save(dict(reconciled, agent_name="QuietCurie"))


def test_server_confirmed_name_cannot_alias_another_local_binding(tmp_path):
    store = IdentityStore(tmp_path)
    first = store.save(
        build_binding(
            session_id="session-one",
            agent_id=None,
            agent_name="Wild-McClintock",
            project_key="/workspace/example",
        )
    )
    store.save(
        build_binding(
            session_id="session-two",
            agent_id=None,
            agent_name="WildMcClintock",
            project_key="/workspace/example",
        )
    )

    with pytest.raises(IdentityStoreError, match="already bound"):
        store.reconcile_agent_name(first["external_id"], "WildMcClintock")


def test_list_bindings_skips_corrupt_files_and_never_reads_tokens(tmp_path):
    store = IdentityStore(tmp_path / "identity")
    binding = store.save(
        build_binding(
            session_id="session-example",
            agent_id=None,
            agent_name="Calm-Noether",
            project_key="/workspace/example",
        )
    )
    store.store_owner_token(binding["external_id"], "owner-secret")
    (store.bindings_dir / "corrupt.json").write_text("{", encoding="utf-8")

    records = store.list_bindings()
    assert records == [binding]
    assert "owner-secret" not in json.dumps(records)
