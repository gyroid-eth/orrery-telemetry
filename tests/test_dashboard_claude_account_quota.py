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
    monkeypatch.setattr(claude_account, "_OPENER", _Opener(_http_error(401)))
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: 1000.0)

    assert provider.read().reason == "sign_in_required"


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
    provider = ClaudeAccountQuotaProvider(token_reader=lambda: "token", clock=lambda: now[0])

    provider.read()
    now[0] += claude_account.BACKOFF_SECONDS * 2
    provider.read()

    assert len(opener.calls) == 1, "the server asked for longer than the floor"


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

    assert snapshot.source == "claude-statusline"
    assert snapshot.buckets
    # The observer answers with fewer windows, so the payload has to say that
    # this is the lesser answer and why the fuller one was missed.
    assert snapshot.status == "degraded"
    assert snapshot.reason == "fallback_rate_limited"


def test_a_transport_failure_still_lets_the_observer_answer():
    """A timeout is the commonest account failure and must not end the read."""
    class _Raising:
        source_name = "claude-account-usage"

        def read(self):
            raise RuntimeError("claude usage probe failed: TimeoutError")

    statusline = _Stub(_ok("claude-statusline"))
    snapshot = ClaudeQuotaRoute(_Raising(), statusline, clock=lambda: 1000.0).read()

    assert statusline.reads == 1
    assert snapshot.status == "degraded"
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
    assert snapshot.status == "degraded"
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
