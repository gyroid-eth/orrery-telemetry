"""The account usage source, and the route that falls back to the observer."""

from __future__ import annotations

import json
import urllib.error

import pytest

from dashboard.quotas import claude_account
from dashboard.quotas.base import QuotaBucket, QuotaSnapshot
from dashboard.quotas.claude_account import ClaudeAccountQuotaProvider, parse_account_usage
from dashboard.quotas.claude_route import ClaudeQuotaRoute


USAGE_BODY = {
    "five_hour": {"utilization": 2, "resets_at": "2026-09-14T11:00:00Z"},
    "seven_day": {"utilization": 42, "resets_at": "2026-09-17T02:00:00Z"},
    "limits": [
        {
            "kind": "weekly_scoped",
            "percent": 77,
            "resets_at": "2026-09-17T08:06:59Z",
            "scope": {"model": {"display_name": "Fable"}},
        },
        {"kind": "five_hour", "percent": 2},
    ],
}


def _provider(**kwargs):
    kwargs.setdefault("token_reader", lambda: "token")
    kwargs.setdefault("fetch", lambda token, timeout: USAGE_BODY)
    kwargs.setdefault("clock", lambda: 1000.0)
    return ClaudeAccountQuotaProvider(**kwargs)


def test_the_account_source_returns_the_per_model_window_the_status_line_never_sends():
    snapshot = _provider().read()

    assert snapshot.status == "ok"
    assert [(b.id, round(b.remaining_percent)) for b in snapshot.buckets] == [
        ("five_hour", 98),
        ("seven_day", 58),
        ("model-fable", 23),
    ]
    assert snapshot.source == "claude-account-usage"


def test_a_limit_that_is_not_model_scoped_is_not_turned_into_a_window():
    body = {"limits": [{"kind": "five_hour", "percent": 10},
                       {"kind": "weekly_scoped", "percent": 10, "scope": {}}]}
    snapshot = parse_account_usage(body, observed_at=1000)

    assert snapshot.status == "unavailable"
    assert snapshot.reason == "no_windows_returned"


def _http_error(code: int, headers: dict[str, str] | None = None):
    def raiser(request, timeout=None):
        raise urllib.error.HTTPError(
            claude_account.USAGE_URL, code, "denied", headers or {}, None)
    return raiser


def test_a_rate_limit_is_answered_once_and_then_stays_quiet(monkeypatch):
    calls = []
    now = [1000.0]

    def urlopen(request, timeout=None):
        calls.append(request)
        _http_error(429, {"Retry-After": "30"})(request)

    monkeypatch.setattr(claude_account.urllib.request, "urlopen", urlopen)
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: now[0])

    first = provider.read()
    now[0] += 60  # past Retry-After, still inside the adapter's own floor
    second = provider.read()

    assert first.reason == second.reason == "rate_limited"
    assert len(calls) == 1, "a 429 must not be retried on the next read"

    now[0] += claude_account.BACKOFF_SECONDS
    provider.read()
    assert len(calls) == 2


def test_an_expired_login_asks_for_sign_in_rather_than_looking_broken(monkeypatch):
    monkeypatch.setattr(claude_account.urllib.request, "urlopen", _http_error(401))
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0)

    assert provider.read().reason == "sign_in_required"


def test_the_request_carries_the_token_as_a_bearer_and_no_body(monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = request.data
        seen["method"] = request.get_method()

        class _Response:
            def read(self):
                return json.dumps(USAGE_BODY).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()

    monkeypatch.setattr(claude_account.urllib.request, "urlopen", urlopen)
    snapshot = ClaudeAccountQuotaProvider(token_reader=lambda: "secret",
                                          clock=lambda: 1000.0).read()

    assert snapshot.status == "ok"
    assert seen["url"] == claude_account.USAGE_URL
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] is None
    assert seen["method"] == "GET"


def test_a_transport_failure_never_carries_the_token_into_the_error():
    def fetch(token, timeout):
        raise urllib.error.URLError(f"connection refused while sending {token}")

    with pytest.raises(RuntimeError) as failure:
        _provider(fetch=fetch).read()

    assert "token" not in str(failure.value)
    assert str(failure.value) == "claude usage probe failed: URLError"


def test_the_operator_can_turn_the_account_source_off(monkeypatch):
    monkeypatch.setenv(claude_account.DISABLE_ENV, "off")
    provider = _provider()

    assert provider.available() is False
    assert provider.read().reason == "account_usage_disabled"


def test_a_machine_without_stored_credentials_asks_for_sign_in():
    provider = _provider(token_reader=lambda: None)

    assert provider.available() is False
    assert provider.read().reason == "sign_in_required"


def test_the_token_is_read_from_the_local_credentials_file(tmp_path, monkeypatch):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "  abc  "}}), encoding="utf-8")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_account, "CREDENTIALS_PATH", str(path))

    assert claude_account._token_from_file(str(path)) == "abc"


class _Stub:
    def __init__(self, snapshot: QuotaSnapshot, available: bool = True) -> None:
        self._snapshot = snapshot
        self._available = available
        self.reads = 0

    def available(self) -> bool:
        return self._available

    def read(self) -> QuotaSnapshot:
        self.reads += 1
        return self._snapshot


def _ok(source: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="claude", source=source, observed_at=1000, status="ok",
        buckets=(QuotaBucket.from_used(id="five_hour", label="5h", scope="account",
                                       used_percent=10, window_seconds=18000, resets_at=None),))


def _down(source: str, reason: str) -> QuotaSnapshot:
    return QuotaSnapshot(provider="claude", source=source, observed_at=1000,
                         status="unavailable", reason=reason)


def test_the_route_prefers_the_account_and_leaves_the_observer_unread():
    statusline = _Stub(_ok("claude-statusline"))
    route = ClaudeQuotaRoute(_Stub(_ok("claude-account-usage")), statusline)

    assert route.read().source == "claude-account-usage"
    assert statusline.reads == 0


def test_a_rate_limited_account_degrades_to_the_observer_rather_than_to_nothing():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "rate_limited")),
                             _Stub(_ok("claude-statusline")))

    snapshot = route.read()

    assert snapshot.status == "ok"
    assert snapshot.source == "claude-statusline"


def test_a_signed_out_machine_with_no_observation_says_sign_in():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "sign_in_required"), available=False),
                             _Stub(_down("claude-statusline", "not_observed")))

    assert route.read().reason == "not_observed"


def test_a_failing_account_read_reports_its_own_reason_when_nothing_was_observed():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "rate_limited")),
                             _Stub(_down("claude-statusline", "not_observed")))

    assert route.read().reason == "rate_limited"
