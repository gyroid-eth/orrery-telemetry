"""The account usage source, and the route that falls back to the observer."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from email.utils import formatdate

import pytest

from dashboard.quotas import claude_account
from dashboard.quotas.base import QuotaBucket, QuotaSnapshot
from dashboard.quotas.claude_account import ClaudeAccountQuotaProvider, parse_account_usage
from dashboard.quotas.claude_route import ClaudeQuotaRoute
from dashboard.quotas.service import QuotaService


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


class _Opener:
    """Stands in for the module's opener so the real request path is exercised."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        return self._handler(request)


def _http_error(code: int, headers: dict[str, str] | None = None):
    def raiser(request):
        raise urllib.error.HTTPError(
            claude_account.USAGE_URL, code, "denied", headers or {}, None)
    return raiser


def _json_response(body):
    class _Response:
        def read(self):
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def handler(request):
        return _Response()

    return handler


def test_a_rate_limit_is_answered_once_and_then_stays_quiet(monkeypatch):
    calls = []
    now = [1000.0]

    opener = _Opener(_http_error(429, {"Retry-After": "30"}))
    calls = opener.calls
    monkeypatch.setattr(claude_account, "_OPENER", opener)
    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token", clock=lambda: now[0], jitter=lambda maximum: 0)

    first = provider.read()
    now[0] += 60  # past Retry-After, still inside the adapter's own floor
    second = provider.read()

    assert first.reason == second.reason == "rate_limited"
    assert len(calls) == 1, "a 429 must not be retried on the next read"

    now[0] += claude_account.BACKOFF_SECONDS
    provider.read()
    assert len(calls) == 2


def test_an_expired_login_asks_for_sign_in_rather_than_looking_broken(monkeypatch):
    monkeypatch.setattr(claude_account, "_OPENER", _Opener(_http_error(401)))
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0)

    assert provider.read().reason == "sign_in_required"


def test_an_expired_login_keeps_its_reason_during_the_outbound_wait(monkeypatch):
    now = [1000.0]
    opener = _Opener(_http_error(401))
    monkeypatch.setattr(claude_account, "_OPENER", opener)
    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )

    assert provider.read().reason == "sign_in_required"
    now[0] = 1030
    cached = provider.read()

    assert cached.reason == "sign_in_required"
    assert len(opener.calls) == 1


def test_the_request_carries_the_token_as_a_bearer_and_no_body(monkeypatch):
    opener = _Opener(_json_response(USAGE_BODY))
    monkeypatch.setattr(claude_account, "_OPENER", opener)

    snapshot = ClaudeAccountQuotaProvider(token_reader=lambda: "secret",
                                          clock=lambda: 1000.0).read()

    request = opener.calls[0]
    assert snapshot.status == "ok"
    assert request.full_url == claude_account.USAGE_URL
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.data is None
    assert request.get_method() == "GET"


def test_a_redirect_is_refused_so_the_token_cannot_follow_it():
    """urllib copies Authorization onto a redirect target; this one must not run."""
    handler = claude_account._NoRedirects()

    assert handler.redirect_request(
        urllib.request.Request(claude_account.USAGE_URL), None, 302, "Found", {},
        "https://example.invalid/") is None


def test_a_retry_after_date_is_honoured_rather_than_read_as_zero(monkeypatch):
    monkeypatch.setattr(claude_account.time, "time", lambda: 1_000_000.0)
    later = formatdate(1_000_000.0 + 900, usegmt=True)

    assert claude_account._retry_after_seconds(later) == pytest.approx(900, abs=2)
    assert claude_account._retry_after_seconds("45") == 45
    assert claude_account._retry_after_seconds("not a date") == 0
    assert claude_account._retry_after_seconds(None) == 0


def test_a_long_retry_after_wins_over_the_adapter_floor(monkeypatch):
    now = [1000.0]
    opener = _Opener(_http_error(429, {"Retry-After": str(claude_account.BACKOFF_SECONDS * 4)}))
    monkeypatch.setattr(claude_account, "_OPENER", opener)
    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token", clock=lambda: now[0], jitter=lambda maximum: 0)

    provider.read()
    now[0] += claude_account.BACKOFF_SECONDS * 2
    provider.read()

    assert len(opener.calls) == 1, "the server asked for longer than the floor"


def test_successful_reads_use_an_outbound_interval_with_positive_jitter():
    now = [1000.0]
    calls = []

    def fetch(token, timeout):
        calls.append(now[0])
        return USAGE_BODY

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=fetch,
        clock=lambda: now[0],
        jitter=lambda maximum: 25,
    )

    assert provider.read().status == "ok"
    now[0] = 1624
    retained = provider.read()
    assert retained.status == "stale"
    assert retained.reason == "account_refresh_scheduled"
    assert retained.observed_at == 1000
    assert len(calls) == 1

    now[0] = 1625
    assert provider.read().status == "ok"
    assert calls == [1000.0, 1625]


def test_consecutive_429s_back_off_exponentially_and_cap_at_one_hour():
    now = [1000.0]
    calls = []

    def limited(token, timeout):
        calls.append(now[0])
        raise claude_account._RateLimited(0)

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=limited,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )

    for due in (1000, 1600, 2800, 5200, 8800, 12400):
        now[0] = due
        provider.read()

    assert calls == [1000, 1600, 2800, 5200, 8800, 12400]
    now[0] = 15999
    provider.read()
    assert len(calls) == 6
    now[0] = 16000
    provider.read()
    assert len(calls) == 7, "the exponential component stays capped at 3600 s"


def test_success_resets_the_429_exponent():
    now = [1000.0]
    outcomes = ["limited", "ok", "limited", "limited"]
    calls = []

    def fetch(token, timeout):
        calls.append(now[0])
        if outcomes.pop(0) == "limited":
            raise claude_account._RateLimited(0)
        return USAGE_BODY

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=fetch,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )

    for due in (1000, 1600, 2200, 2800):
        now[0] = due
        provider.read()

    assert calls == [1000, 1600, 2200, 2800]


def test_credential_rotation_does_not_reset_the_machine_wide_429_exponent():
    now = [1000.0]
    token = ["account-a"]
    calls = []

    def limited(value, timeout):
        calls.append((value, now[0]))
        raise claude_account._RateLimited(0)

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: token[0],
        fetch=limited,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )

    provider.read()  # 600 s
    now[0] = 1600
    provider.read()  # 1200 s
    token[0] = "account-b"
    now[0] = 1700
    assert provider.read().reason == "account_identity_changed"
    now[0] = 2800
    provider.read()  # still the third failure: 2400 s
    now[0] = 5199
    provider.read()

    assert calls == [
        ("account-a", 1000.0),
        ("account-a", 1600),
        ("account-b", 2800),
    ]
    now[0] = 5200
    provider.read()
    assert calls[-1] == ("account-b", 5200)


def test_retry_after_longer_than_the_cap_is_not_shortened():
    now = [1000.0]
    calls = []

    def limited(token, timeout):
        calls.append(now[0])
        raise claude_account._RateLimited(7200)

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=limited,
        clock=lambda: now[0],
        jitter=lambda maximum: 15,
    )

    provider.read()
    now[0] = 8214
    provider.read()
    assert len(calls) == 1
    now[0] = 8215
    provider.read()
    assert len(calls) == 2


def test_sign_out_and_credential_change_never_reuse_the_old_snapshot():
    now = [1000.0]
    token = ["account-a"]
    outcomes = [USAGE_BODY]
    calls = []

    def fetch(value, timeout):
        calls.append((value, now[0]))
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    provider = ClaudeAccountQuotaProvider(
        token_reader=lambda: token[0],
        fetch=fetch,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )
    assert provider.read().buckets

    token[0] = "account-b"
    now[0] = 1001
    changed = provider.read()
    assert changed.reason == "account_identity_changed"
    assert changed.buckets == ()
    assert calls == [("account-a", 1000.0)], "a credential change must not bypass the budget"

    token[0] = ""
    signed_out = provider.read()
    assert signed_out.reason == "sign_in_required"
    assert signed_out.buckets == ()


def test_failed_first_fetch_for_a_changed_credential_keeps_the_identity_gate():
    now = [1000.0]
    token = ["account-a"]
    outcomes = [USAGE_BODY, TimeoutError("offline")]

    def fetch(value, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    account = ClaudeAccountQuotaProvider(
        token_reader=lambda: token[0],
        fetch=fetch,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )
    observer = _Stub(_ok("claude-statusline"))
    route = ClaudeQuotaRoute(account, observer, clock=lambda: now[0])
    assert route.read().buckets

    token[0] = "account-b"
    now[0] = 1600
    changed = route.read()

    assert changed.reason == "account_identity_changed"
    assert changed.buckets == ()
    assert observer.reads == 1, "the prior account's observer must stay behind the identity gate"


def test_two_models_whose_names_collide_both_keep_a_window():
    body = {"limits": [
        {"kind": "weekly_scoped", "percent": 10, "scope": {"model": {"display_name": "A/B"}}},
        {"kind": "weekly_scoped", "percent": 90, "scope": {"model": {"display_name": "A-B"}}},
    ]}

    snapshot = parse_account_usage(body, observed_at=1000)

    assert [(b.id, b.label, round(b.remaining_percent)) for b in snapshot.buckets] == [
        ("model-a-b", "A/B", 90),
        ("model-a-b-2", "A-B", 10),
    ]


def test_the_model_id_is_preferred_over_the_display_name_for_the_window_id():
    body = {"limits": [{"kind": "weekly_scoped", "percent": 10,
                        "scope": {"model": {"id": "claude-fable-5-1", "display_name": "Fable"}}}]}

    snapshot = parse_account_usage(body, observed_at=1000)

    assert snapshot.buckets[0].id == "model-claude-fable-5-1"
    assert snapshot.buckets[0].label == "Fable"


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

    assert provider.read().reason == "account_usage_disabled"


def test_a_machine_without_stored_credentials_asks_for_sign_in():
    provider = _provider(token_reader=lambda: None)

    assert provider.read().reason == "sign_in_required"


def test_the_token_is_read_from_the_local_credentials_file(tmp_path, monkeypatch):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "  abc  "}}), encoding="utf-8")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_account, "CREDENTIALS_PATH", str(path))

    assert claude_account._token_from_file(str(path)) == "abc"


class _Stub:
    source_name = "stub"

    def __init__(self, snapshot: QuotaSnapshot) -> None:
        self._snapshot = snapshot
        self.reads = 0

    def read(self) -> QuotaSnapshot:
        self.reads += 1
        return self._snapshot


def _ok(source: str, observed_at: int = 1000) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="claude", source=source, observed_at=observed_at, status="ok",
        buckets=(QuotaBucket.from_used(id="five_hour", label="5h", scope="account",
                                       used_percent=10, window_seconds=18000, resets_at=None),))


def _down(source: str, reason: str) -> QuotaSnapshot:
    return QuotaSnapshot(provider="claude", source=source, observed_at=1000,
                         status="unavailable", reason=reason)


def test_the_route_checks_the_observer_without_replacing_a_newer_account_window():
    statusline = _Stub(_ok("claude-statusline"))
    route = ClaudeQuotaRoute(
        _Stub(_ok("claude-account-usage", observed_at=1001)),
        statusline,
        clock=lambda: 1001.0,
    )

    assert route.read().source == "claude-account-usage"
    assert statusline.reads == 1


def test_a_rate_limited_account_degrades_to_the_observer_rather_than_to_nothing():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "rate_limited")),
                             _Stub(_ok("claude-statusline")), clock=lambda: 1000.0)

    snapshot = route.read()

    assert snapshot.source == "claude-statusline"
    assert snapshot.buckets
    # The observer answers with fewer windows, so the payload has to say that
    # this is the lesser answer and why the fuller one was missed.
    assert snapshot.status == "ok"
    assert snapshot.degraded is True
    assert snapshot.partial is True
    assert snapshot.reason == "fallback_rate_limited"


def test_current_observer_windows_mix_with_previous_account_only_windows():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=(
            QuotaBucket.from_used(
                id="five_hour", label="5h", scope="account", used_percent=20,
                window_seconds=18000, resets_at=5000),
            QuotaBucket.from_used(
                id="model-fable", label="Fable", scope="account", used_percent=70,
                window_seconds=604800, resets_at=5000),
        ),
    )
    observer = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1060,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="five_hour", label="5h", scope="account", used_percent=25,
            window_seconds=18000, resets_at=5000),),
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account), _Stub(observer), clock=lambda: 1060.0).read()

    five_hour, fable = snapshot.buckets
    assert (five_hour.source, five_hour.observed_at, five_hour.value_status) == (
        "claude-statusline", 1060, "current")
    assert (fable.source, fable.observed_at, fable.value_status) == (
        "claude-account-usage", 1000, "previous")
    assert snapshot.status == "ok"
    assert snapshot.degraded is True
    assert snapshot.partial is False, "retained Fable means no known window is missing"


@pytest.mark.parametrize(("now", "reset", "why"), [
    (2800, 5000, "30 minute retention"),
    (1100, 1100, "window reset"),
])
def test_an_expired_account_window_keeps_its_name_but_not_its_value(now, reset, why):
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=reset),),
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account),
        _Stub(_down("claude-statusline", "not_observed")),
        clock=lambda: float(now),
    ).read()
    bucket = snapshot.buckets[0]
    wire = snapshot.to_dict()["buckets"][0]

    assert bucket.label == "Fable", why
    assert bucket.observed_at == 1000
    assert bucket.value_status == "unknown"
    assert "used_percent" not in wire
    assert "remaining_percent" not in wire
    assert wire["source"] == "claude-account-usage"


def test_freshness_and_degradation_can_be_true_at_the_same_time():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )
    snapshot = ClaudeQuotaRoute(
        _Stub(account),
        _Stub(_down("claude-statusline", "not_observed")),
        clock=lambda: 1060.0,
    ).read()

    assert snapshot.status == "stale"
    assert snapshot.degraded is True
    assert snapshot.partial is False


def test_normal_account_wait_is_previous_but_not_degraded():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="account_refresh_scheduled",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )
    snapshot = ClaudeQuotaRoute(
        _Stub(account),
        _Stub(_ok("claude-statusline", observed_at=1060)),
        clock=lambda: 1060.0,
    ).read()

    fable = next(bucket for bucket in snapshot.buckets if bucket.id == "model-fable")
    assert fable.value_status == "previous"
    assert snapshot.status == "ok"
    assert snapshot.degraded is False
    assert snapshot.partial is False


def test_a_transport_failure_still_lets_the_observer_answer():
    """A timeout is the commonest account failure and must not end the read."""
    class _Raising:
        source_name = "claude-account-usage"

        def read(self):
            raise RuntimeError("claude usage probe failed: TimeoutError")

    statusline = _Stub(_ok("claude-statusline"))
    snapshot = ClaudeQuotaRoute(_Raising(), statusline, clock=lambda: 1000.0).read()

    assert statusline.reads == 1
    assert snapshot.status == "ok"
    assert snapshot.degraded is True
    assert snapshot.partial is True
    assert snapshot.reason == "fallback_account_usage_failed"


def test_a_transport_failure_with_no_observation_reports_the_account_failure():
    class _Raising:
        source_name = "claude-account-usage"

        def read(self):
            raise RuntimeError("claude usage probe failed: URLError")

    route = ClaudeQuotaRoute(_Raising(), _Stub(_down("claude-statusline", "not_observed")),
                             clock=lambda: 1000.0)

    assert route.read().reason == "account_usage_failed"


def test_a_signed_out_machine_reports_that_rather_than_the_observer_silence():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "sign_in_required")),
                             _Stub(_down("claude-statusline", "not_observed")))

    # Both are down, and the account's reason is the one a person can act on.
    assert route.read().reason == "sign_in_required"


@pytest.mark.parametrize("reason", ["sign_in_required", "account_identity_changed"])
def test_uncertain_account_identity_never_reuses_an_observer_snapshot(reason):
    route = ClaudeQuotaRoute(
        _Stub(_down("claude-account-usage", reason)),
        _Stub(_ok("claude-statusline", observed_at=1060)),
        clock=lambda: 1060.0,
    )

    snapshot = route.read()

    assert snapshot.status == "unavailable"
    assert snapshot.reason == reason
    assert snapshot.buckets == ()


def test_account_observation_rejects_older_observer_only_model_windows():
    account = _ok("claude-account-usage", observed_at=1000)
    old_observer = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=999,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account), _Stub(old_observer), clock=lambda: 1000.0).read()

    assert [bucket.id for bucket in snapshot.buckets] == ["five_hour"]


def test_authenticated_empty_account_response_rejects_an_older_observer():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="unavailable",
        reason="no_windows_returned",
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account),
        _Stub(_ok("claude-statusline", observed_at=999)),
        clock=lambda: 1000.0,
    ).read()

    assert snapshot.reason == "no_windows_returned"
    assert snapshot.buckets == ()


def test_authenticated_empty_account_floor_survives_the_next_local_read():
    now = [1000.0]
    calls = []

    def empty_usage(token, timeout):
        calls.append(now[0])
        return {}

    account = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=empty_usage,
        clock=lambda: now[0],
        jitter=lambda maximum: 0,
    )
    observer = _Stub(_ok("claude-statusline", observed_at=999))
    route = ClaudeQuotaRoute(account, observer, clock=lambda: now[0])

    assert route.read().buckets == ()
    now[0] = 1030
    second = route.read()

    assert second.reason == "no_windows_returned"
    assert second.buckets == ()
    assert calls == [1000.0]


def test_retained_empty_account_floor_does_not_erase_a_newer_observer_window():
    now = [1000.0]
    account = ClaudeAccountQuotaProvider(
        token_reader=lambda: "token",
        fetch=lambda token, timeout: {},
        clock=lambda: now[0],
        jitter=lambda maximum: 60,
    )
    observer = _Stub(_down("claude-statusline", "not_observed"))
    route = ClaudeQuotaRoute(account, observer, clock=lambda: now[0])
    assert route.read().buckets == ()

    observer._snapshot = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1010,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )
    now[0] = 1030
    assert route.read().buckets[0].value_status == "current"

    observer._snapshot = _down("claude-statusline", "observation_stale")
    now[0] = 1611  # observer is >600 s old; account's 600+60 s budget is not due
    retained = route.read().buckets[0]

    assert (retained.id, retained.observed_at, retained.value_status) == (
        "model-fable", 1010, "previous")


def test_observer_only_window_remains_named_after_the_observer_file_expires():
    now = [1000.0]
    fable = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1000,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )
    observer = _Stub(fable)
    route = ClaudeQuotaRoute(
        _Stub(_down("claude-account-usage", "rate_limited")),
        observer,
        clock=lambda: now[0],
    )
    assert route.read().buckets[0].value_status == "current"

    observer._snapshot = _down("claude-statusline", "observation_stale")
    now[0] = 1601
    retained = route.read().buckets[0]
    assert (retained.id, retained.observed_at, retained.value_status) == (
        "model-fable", 1000, "previous")

    now[0] = 2800
    expired = route.read().buckets[0]
    assert (expired.id, expired.observed_at, expired.value_status) == (
        "model-fable", 1000, "unknown")


def test_stable_account_model_id_wins_when_observer_only_has_the_display_name():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=(QuotaBucket.from_used(
            id="model-claude-fable-5-1", label="Fable", scope="account", used_percent=70,
            window_seconds=604800, resets_at=5000),),
    )
    observer = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1060,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="  fable  ", scope="account", used_percent=75,
            window_seconds=604800, resets_at=5000),),
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account), _Stub(observer), clock=lambda: 1060.0).read()

    assert len(snapshot.buckets) == 1
    bucket = snapshot.buckets[0]
    assert bucket.id == "model-claude-fable-5-1"
    assert (bucket.source, bucket.observed_at, round(bucket.remaining_percent)) == (
        "claude-statusline", 1060, 25)


def test_ambiguous_display_names_are_not_guessed_to_be_the_same_model():
    account = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=tuple(
            QuotaBucket.from_used(
                id=bucket_id, label="Fable", scope="account", used_percent=used,
                window_seconds=604800, resets_at=5000)
            for bucket_id, used in (
                ("model-claude-fable-5-1", 60),
                ("model-claude-fable-5-2", 70),
            )
        ),
    )
    observer = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1060,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=75,
            window_seconds=604800, resets_at=5000),),
    )

    snapshot = ClaudeQuotaRoute(
        _Stub(account), _Stub(observer), clock=lambda: 1060.0).read()

    assert {bucket.id for bucket in snapshot.buckets} == {
        "model-claude-fable-5-1",
        "model-claude-fable-5-2",
        "model-fable",
    }


def test_an_existing_observer_window_does_not_make_ambiguous_account_names_unique():
    now = [1000.0]
    observer = QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=1000,
        status="ok",
        buckets=(QuotaBucket.from_used(
            id="model-fable", label="Fable", scope="account", used_percent=75,
            window_seconds=604800, resets_at=5000),),
    )
    account = _Stub(_down("claude-account-usage", "rate_limited"))
    statusline = _Stub(observer)
    route = ClaudeQuotaRoute(account, statusline, clock=lambda: now[0])
    assert [bucket.id for bucket in route.read().buckets] == ["model-fable"]

    account._snapshot = QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=1000,
        status="stale",
        reason="rate_limited",
        buckets=tuple(
            QuotaBucket.from_used(
                id=bucket_id, label="Fable", scope="account", used_percent=used,
                window_seconds=604800, resets_at=5000)
            for bucket_id, used in (
                ("model-claude-fable-5-1", 60),
                ("model-claude-fable-5-2", 70),
            )
        ),
    )
    statusline._snapshot = _down("claude-statusline", "observation_stale")
    now[0] = 1030

    assert {bucket.id for bucket in route.read().buckets} == {
        "model-fable",
        "model-claude-fable-5-1",
        "model-claude-fable-5-2",
    }


def test_sign_in_clears_prior_windows_before_a_broken_observer_is_read():
    class _Observer:
        source_name = "claude-statusline"

        def __init__(self):
            self.reads = 0
            self.fail = False

        def read(self):
            self.reads += 1
            if self.fail:
                raise RuntimeError("broken status-line file")
            return _ok(self.source_name)

    account = _Stub(_ok("claude-account-usage"))
    observer = _Observer()
    route = ClaudeQuotaRoute(account, observer, clock=lambda: 1000.0)
    now = [1000.0]
    route._clock = lambda: now[0]
    service = QuotaService([route], clock=lambda: now[0])
    assert service.read_all()["providers"][0]["buckets"]

    account._snapshot = _down("claude-account-usage", "sign_in_required")
    observer.fail = True
    now[0] = 1030
    signed_out = service.read_all()["providers"][0]

    assert signed_out["reason"] == "sign_in_required"
    assert signed_out["buckets"] == []
    assert signed_out["last_observed_at"] is None
    assert observer.reads == 1, "identity rejection must happen before observer I/O"


def test_a_rate_limited_account_reports_its_own_reason_when_nothing_was_observed():
    route = ClaudeQuotaRoute(_Stub(_down("claude-account-usage", "rate_limited")),
                             _Stub(_down("claude-statusline", "not_observed")))

    assert route.read().reason == "rate_limited"


def test_a_server_error_reaches_the_route_as_a_failure_the_observer_can_follow(monkeypatch):
    """5xx must travel the same path as a timeout: raised, then fallen back on."""
    monkeypatch.setattr(claude_account, "_OPENER", _Opener(_http_error(503)))
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0)

    with pytest.raises(RuntimeError):
        provider.read()

    statusline = _Stub(_ok("claude-statusline"))
    snapshot = ClaudeQuotaRoute(provider, statusline, clock=lambda: 1000.0).read()
    assert statusline.reads == 1
    assert snapshot.status == "ok"
    assert snapshot.degraded is True
    assert snapshot.partial is True
    assert snapshot.reason == "fallback_account_usage_failed"


def test_an_unreadable_body_is_a_failure_rather_than_an_empty_account(monkeypatch):
    class _Garbage:
        def read(self):
            return b"<html>maintenance</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(claude_account, "_OPENER", _Opener(lambda request: _Garbage()))

    with pytest.raises(RuntimeError):
        ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0).read()


def test_the_opener_in_use_is_the_one_that_refuses_redirects():
    """Wiring, not just the handler: a redirect must not be followed."""
    import http.server
    import threading

    hits = {"source": 0, "target": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/target":
                hits["target"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            hits["source"] += 1
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/source",
            headers={"Authorization": "Bearer secret"})
        with pytest.raises(urllib.error.HTTPError) as redirect:
            claude_account._OPENER.open(request, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert redirect.value.code == 302
    assert hits == {"source": 1, "target": 0}


def test_a_window_the_payload_repeats_is_drawn_once():
    entry = {"kind": "weekly_scoped", "percent": 10, "resets_at": "2026-09-17T08:06:59Z",
             "scope": {"model": {"display_name": "Fable"}}}
    snapshot = parse_account_usage({"limits": [entry, dict(entry)]}, observed_at=1000)

    assert [b.id for b in snapshot.buckets] == ["model-fable"]


def test_a_reset_time_is_carried_as_the_epoch_it_names():
    body = {"five_hour": {"utilization": 10, "resets_at": "2026-09-14T11:00:00Z"}}
    snapshot = parse_account_usage(body, observed_at=1000)

    assert snapshot.buckets[0].resets_at == 1789383600


@pytest.mark.parametrize("value", [True, False, None, "40", float("nan"), float("inf")])
def test_a_percentage_that_is_not_a_number_is_not_turned_into_a_window(value):
    snapshot = parse_account_usage({"five_hour": {"utilization": value}}, observed_at=1000)

    assert snapshot.status == "unavailable"


@pytest.mark.parametrize(("used", "remaining"), [(-20, 100), (140, 0)])
def test_a_percentage_outside_the_scale_is_clamped(used, remaining):
    snapshot = parse_account_usage({"five_hour": {"utilization": used}}, observed_at=1000)

    assert round(snapshot.buckets[0].remaining_percent) == remaining


def test_with_the_account_source_off_a_window_does_not_outlive_its_account(monkeypatch):
    """Off, nothing reads the credentials, so a sign-out cannot be noticed.

    The observer's file carries no account identity either, so a window held
    across reads could belong to an account that is no longer signed in.
    """
    monkeypatch.setenv(claude_account.DISABLE_ENV, "off")
    account = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0)

    first = QuotaSnapshot(
        provider="claude", source="claude-statusline", observed_at=1000, status="ok",
        buckets=(QuotaBucket.from_used(id="model-fable", label="Fable", scope="account",
                                       used_percent=70, window_seconds=604800, resets_at=9_000_000),))
    second = QuotaSnapshot(
        provider="claude", source="claude-statusline", observed_at=1100, status="ok",
        buckets=(QuotaBucket.from_used(id="five_hour", label="5h", scope="account",
                                       used_percent=10, window_seconds=18000, resets_at=9_000_000),))

    observer = _Stub(first)
    route = ClaudeQuotaRoute(account, observer, clock=lambda: 1000.0)
    assert [b.id for b in route.read().buckets] == ["model-fable"]

    observer._snapshot = second
    assert [b.id for b in route.read().buckets] == ["five_hour"], \
        "a window from the previous read must not survive into the next one"
