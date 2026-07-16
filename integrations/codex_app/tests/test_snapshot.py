from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from agentstack_codex_app.identity_store import build_binding
from agentstack_codex_app.snapshot import (
    SnapshotError,
    SnapshotStore,
    read_snapshot,
    runtime_record,
    write_snapshot,
)


def test_snapshot_is_sanitized_atomic_and_private(tmp_path):
    binding = build_binding(
        session_id="session-example",
        agent_id=None,
        agent_name="Calm-Noether",
        project_key="/workspace/example",
    )
    event = {
        "model": "gpt-example",
        "cwd": "/workspace/example",
        "prompt": "must not be copied",
        "tool_input": {"secret": "must not be copied"},
    }
    record = runtime_record(binding, event, state="working")
    path = tmp_path / "runtime" / "snapshot.json"
    write_snapshot(path, [record])

    text = path.read_text(encoding="utf-8")
    assert "must not be copied" not in text
    assert "token" not in text.lower()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert read_snapshot(path)["runtimes"][0]["state"] == "working"
    assert read_snapshot(path)["runtimes"][0]["delivery"]["wake_status"] == "idle"


def test_snapshot_rejects_non_allowlisted_fields(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-01-01T00:00:00Z",
                "runtimes": [{"external_id": "codex:x", "secret": "bad"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotError):
        read_snapshot(path)


def test_snapshot_exposes_only_sanitized_delivery_failure_codes(tmp_path):
    binding = build_binding(
        session_id="session-example",
        agent_id=None,
        agent_name="Calm-Noether",
        project_key="/workspace/example",
    )
    record = runtime_record(binding, {}, state="waiting")
    record["delivery"] = {
        "pending_count": 1,
        "wake_status": "wake_failed",
        "failed_count": 1,
        "dead_letter_count": 0,
        "last_error": "resume_failed",
        "parent_external_id": None,
    }
    path = tmp_path / "snapshot.json"
    write_snapshot(path, [record])
    assert (
        read_snapshot(path)["runtimes"][0]["delivery"]["last_error"]
        == "resume_failed"
    )

    record["delivery"]["last_error"] = "private stderr: approval details"
    with pytest.raises(SnapshotError):
        write_snapshot(path, [record])


def test_stale_waiting_runtime_becomes_dormant_but_active_runtime_does_not(
    tmp_path,
):
    path = tmp_path / "snapshot.json"
    stale = build_binding(
        session_id="session-stale",
        agent_id=None,
        agent_name="CalmNoether",
        project_key="/workspace/example",
        now="2026-01-01T00:00:00Z",
    )
    active = build_binding(
        session_id="session-active",
        agent_id=None,
        agent_name="QuietCurie",
        project_key="/workspace/example",
        now="2026-01-01T00:00:00Z",
    )
    write_snapshot(
        path,
        [
            runtime_record(
                stale,
                {},
                state="waiting",
                last_seen_at="2026-01-01T00:00:00Z",
            ),
            runtime_record(
                active,
                {},
                state="working",
                last_seen_at="2026-01-01T00:00:00Z",
            ),
        ],
    )

    changed = SnapshotStore(path).mark_waiting_dormant_older_than(
        3600,
        now=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    )
    runtimes = {
        item["session_id"]: item
        for item in read_snapshot(path)["runtimes"]
    }

    assert changed == 1
    assert runtimes["session-stale"]["state"] == "dormant"
    assert runtimes["session-active"]["state"] == "working"


def test_snapshot_remove_is_idempotent(tmp_path):
    binding = build_binding(
        session_id="session-example",
        agent_id=None,
        agent_name="CalmNoether",
        project_key="/workspace/example",
    )
    store = SnapshotStore(tmp_path / "snapshot.json")
    store.upsert(runtime_record(binding, {}, state="waiting"))

    assert store.remove(binding["external_id"]) is True
    assert store.remove(binding["external_id"]) is False
    assert read_snapshot(store.path)["runtimes"] == []
