from __future__ import annotations

import json
from pathlib import Path

from dashboard.providers.codex_app import CodexAppRuntimeProvider


FIXTURE = Path(__file__).parent / "fixtures" / "codex-app-snapshot.json"


def test_provider_reads_sanitized_bridge_snapshot():
    provider = CodexAppRuntimeProvider(FIXTURE, open_adapter=lambda: {"ok": True})
    runtimes = provider.list_runtimes()
    assert len(runtimes) == 1
    assert runtimes[0].external_id == "codex:session-example"
    assert runtimes[0].provider == "codex-app"
    assert runtimes[0].state == "waiting"
    assert runtimes[0].capabilities == frozenset({"open"})
    assert runtimes[0].metadata["delivery"]["wake_status"] == "idle"


def test_provider_open_activates_app_without_deep_link():
    calls = []

    def activate():
        calls.append(True)
        return {"ok": True, "adapter": "test-app-activate"}

    provider = CodexAppRuntimeProvider(FIXTURE, open_adapter=activate)
    result = provider.perform("codex:session-example", "open")
    assert result.ok is True
    assert calls == [True]


def test_provider_does_not_offer_wake_or_open_unknown_runtime():
    provider = CodexAppRuntimeProvider(FIXTURE, open_adapter=lambda: {"ok": True})
    assert provider.perform("codex:session-example", "wake").ok is False
    assert provider.perform("codex:unknown", "open").error == "unknown Codex App runtime"


def test_provider_rejects_snapshot_records_with_non_allowlisted_fields(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["runtimes"][0]["owner_token"] = "must-not-reach-dashboard"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    provider = CodexAppRuntimeProvider(path, open_adapter=lambda: {"ok": True})
    assert provider.list_runtimes() == []


def test_provider_exposes_sanitized_wake_failure_and_dead_letter_counts(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["runtimes"][0]["delivery"] = {
        "pending_count": 1,
        "wake_status": "wake_failed",
        "failed_count": 0,
        "dead_letter_count": 1,
        "last_error": "resume_failed",
        "parent_external_id": None,
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    runtime = CodexAppRuntimeProvider(path).list_runtimes()[0]
    assert runtime.metadata["delivery"]["wake_status"] == "wake_failed"
    assert runtime.metadata["delivery"]["dead_letter_count"] == 1
