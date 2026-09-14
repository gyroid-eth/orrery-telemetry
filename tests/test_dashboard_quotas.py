from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from dashboard.quotas.antigravity import parse_antigravity_usage
from dashboard.quotas.base import QuotaBucket, QuotaSnapshot
from dashboard.quotas.claude import ClaudeQuotaProvider, parse_claude_statusline
from dashboard.quotas.codex import parse_codex_rate_limits
from dashboard.quotas.service import QuotaService


def test_claude_statusline_normalizes_subscription_windows():
    snapshot = parse_claude_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 2000},
                "seven_day": {"used_percentage": 41.2, "resets_at": 3000},
            }
        },
        observed_at=1000,
    )

    assert snapshot.status == "ok"
    assert [bucket.label for bucket in snapshot.buckets] == ["5h", "7d"]
    assert [bucket.remaining_percent for bucket in snapshot.buckets] == [76.5, 58.8]


def test_claude_statusline_reads_model_scoped_weekly_windows():
    # Claude Code 2.1.268 appends per-model weekly windows unscaled: utilization
    # is a 0..1 fraction and resets_at an ISO-8601 string.
    snapshot = parse_claude_statusline(
        {
            "rate_limits": {
                "seven_day": {"used_percentage": 41.2, "resets_at": 3000},
                "model_scoped": [
                    {"display_name": "Fable", "utilization": 0.67, "resets_at": "1970-01-01T00:50:00Z"},
                    {"display_name": "", "utilization": 0.1, "resets_at": None},
                    {"display_name": "Nullish", "utilization": None, "resets_at": None},
                    "junk",
                ],
            }
        },
        observed_at=1000,
    )

    assert [bucket.id for bucket in snapshot.buckets] == ["seven_day", "model-fable"]
    fable = snapshot.buckets[1]
    assert fable.label == "Fable"
    assert fable.remaining_percent == 33.0
    assert fable.window_seconds == 7 * 24 * 60 * 60
    assert fable.resets_at == 3000


def test_claude_provider_expires_old_observation(tmp_path):
    path = tmp_path / "claude-quota.json"
    path.write_text(
        '{"observed_at":1000,"rate_limits":{"five_hour":{"used_percentage":25,"resets_at":2000}}}',
        encoding="utf-8",
    )
    now = [1005.0]
    provider = ClaudeQuotaProvider(path, max_age_seconds=10, clock=lambda: now[0])

    assert provider.read().status == "ok"
    now[0] = 1011.0
    expired = provider.read()
    assert expired.status == "unavailable"
    assert expired.reason == "observation_stale"
    assert expired.buckets == ()
    assert expired.observed_at == 1000


def test_codex_uses_window_duration_not_primary_secondary_position():
    snapshot = parse_codex_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 10080,
                    "resetsAt": 3000,
                },
                "secondary": {
                    "usedPercent": 20,
                    "windowDurationMins": 300,
                    "resetsAt": 1500,
                },
            }
        },
        observed_at=1000,
    )

    assert [bucket.label for bucket in snapshot.buckets] == ["5h", "7d"]
    assert [bucket.remaining_percent for bucket in snapshot.buckets] == [80.0, 69.0]


def test_codex_does_not_invent_missing_window():
    snapshot = parse_codex_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 10080,
                    "resetsAt": 3000,
                },
                "secondary": None,
            }
        },
        observed_at=1000,
    )

    assert [bucket.label for bucket in snapshot.buckets] == ["7d"]


def test_antigravity_uses_only_returned_dynamic_buckets():
    snapshot = parse_antigravity_usage(
        {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "displayName": "Five Hour Limit",
                                "remainingFraction": 0.41,
                                "resetTime": "2026-09-11T18:00:00Z",
                            },
                            {
                                "bucketId": "gemini-weekly",
                                "displayName": "Weekly Limit",
                                "remainingFraction": 0.73,
                                "resetTime": "2026-09-14T00:00:00Z",
                            },
                            {
                                "bucketId": "disabled",
                                "remainingFraction": 1.0,
                                "disabled": True,
                            },
                        ],
                    }
                ]
            }
        },
        observed_at=1000,
    )

    assert snapshot.status == "ok"
    assert [bucket.id for bucket in snapshot.buckets] == ["gemini-5h", "gemini-weekly"]
    assert [bucket.label for bucket in snapshot.buckets] == [
        "Gemini Models · 5h",
        "Gemini Models · 7d",
    ]
    assert [bucket.remaining_percent for bucket in snapshot.buckets] == [41.0, 73.0]


@pytest.mark.parametrize("fraction", [1.01, 58, -0.01, True, float("nan"), float("inf")])
def test_antigravity_rejects_invalid_fractions_without_guessing_units(fraction):
    snapshot = parse_antigravity_usage(
        {"groups": [{"buckets": [
            {"id": "invalid", "remainingFraction": fraction},
            {"id": "valid", "remainingFraction": 0},
        ]}]},
        observed_at=1000,
    )
    assert [bucket.id for bucket in snapshot.buckets] == ["valid"]
    assert snapshot.buckets[0].remaining_percent == 0


@dataclass
class _Provider:
    provider_name: str
    source_name: str
    result: QuotaSnapshot | Exception
    ttl_seconds: int = 60
    calls: int = 0

    def read(self) -> QuotaSnapshot:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _ok_snapshot(provider: str, observed_at: int = 1000) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider=provider,
        source=f"{provider}-source",
        observed_at=observed_at,
        status="ok",
        buckets=(
            QuotaBucket.from_remaining(
                id="5h",
                label="5h",
                scope="account",
                remaining_percent=75,
                window_seconds=18000,
                resets_at=observed_at + 100,
            ),
        ),
    )


def test_service_isolates_provider_failure_and_honors_ttl():
    now = [1000.0]
    good = _Provider("good", "good-source", _ok_snapshot("good"))
    bad = _Provider("bad", "bad-source", RuntimeError("offline"))
    service = QuotaService([good, bad], clock=lambda: now[0])

    first = service.read_all()
    second = service.read_all()

    assert first["degraded"] is True
    assert [item["status"] for item in first["providers"]] == ["ok", "unavailable"]
    assert good.calls == 1
    assert bad.calls == 1
    assert second == first


def test_service_uses_recent_success_as_bounded_stale_value():
    now = [1000.0]
    provider = _Provider("codex", "codex-source", _ok_snapshot("codex"), ttl_seconds=1)
    service = QuotaService([provider], stale_seconds=10, clock=lambda: now[0])
    assert service.read_all()["providers"][0]["status"] == "ok"

    provider.result = RuntimeError("temporary failure")
    now[0] = 1002.0
    stale = service.read_all()["providers"][0]
    assert stale["status"] == "stale"
    assert stale["buckets"][0]["remaining_percent"] == 75.0

    now[0] = 1012.0
    unavailable = service.read_all()["providers"][0]
    assert unavailable["status"] == "unavailable"
    assert unavailable["buckets"] == []


def test_normal_cache_cannot_extend_claude_observation_lifetime(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps({"observed_at": 1000, "rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": 2000},
        }}),
        encoding="utf-8",
    )
    now = [1599.0]
    provider = ClaudeQuotaProvider(path, clock=lambda: now[0])
    service = QuotaService([provider], clock=lambda: now[0])
    assert service.read_all()["providers"][0]["status"] == "ok"

    now[0] = 1601.0
    expired = service.read_all()["providers"][0]
    assert expired["status"] == "unavailable"
    assert expired["buckets"] == []
    assert expired["last_observed_at"] == 1000


def test_service_marks_pre_reset_value_stale_without_inventing_new_balance():
    now = [1000.0]
    provider = _Provider("codex", "codex-source", _ok_snapshot("codex"), ttl_seconds=180)
    service = QuotaService([provider], clock=lambda: now[0])
    assert service.read_all()["providers"][0]["status"] == "ok"

    now[0] = 1101.0
    snapshot = service.read_all()["providers"][0]
    assert snapshot["status"] == "stale"
    assert snapshot["reason"] == "window_reset_pending"
    assert snapshot["buckets"][0]["remaining_percent"] == 75
    assert provider.calls == 1


def test_future_observation_is_not_accepted_as_fresh(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps({"observed_at": 2000, "rate_limits": {
            "five_hour": {"used_percentage": 25},
        }}),
        encoding="utf-8",
    )
    assert ClaudeQuotaProvider(path, clock=lambda: 1000).read().status == "unavailable"

    provider = _Provider("codex", "fixture", _ok_snapshot("codex", 2000))
    payload = QuotaService([provider], clock=lambda: 1000).read_all()["providers"][0]
    assert payload["status"] == "unavailable"
    assert payload["buckets"] == []


def test_provider_exception_details_do_not_escape_payload_or_logs(caplog):
    provider = _Provider(
        "codex",
        "fixture",
        RuntimeError("secret-account-token-should-not-leak"),
        ttl_seconds=1,
    )
    payload = QuotaService([provider], clock=lambda: 1000.0).read_all()

    assert payload["providers"][0]["reason"] == "provider_read_failed"
    assert "secret-account-token" not in json.dumps(payload)
    assert "secret-account-token" not in caplog.text
    assert "RuntimeError" in caplog.text
