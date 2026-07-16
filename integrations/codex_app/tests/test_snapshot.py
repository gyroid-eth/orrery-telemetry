from __future__ import annotations

import json
import os

import pytest

from agentstack_codex_app.identity_store import build_binding
from agentstack_codex_app.snapshot import (
    SnapshotError,
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
