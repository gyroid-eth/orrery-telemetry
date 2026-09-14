from __future__ import annotations

from http.server import ThreadingHTTPServer
import importlib
import json
import threading
import urllib.error
import urllib.request

import dashboard.server as dashboard_server


class _FakeQuotaService:
    def __init__(self) -> None:
        self.calls = 0

    def read_all(self) -> dict[str, object]:
        self.calls += 1
        return {
            "ts": 1000,
            "degraded": True,
            "providers": [
                {
                    "provider": "codex",
                    "status": "ok",
                    "source": "fixture",
                    "observed_at": 999,
                    "last_observed_at": 999,
                    "checked_at": 1000,
                    "buckets": [
                        {
                            "id": "5h",
                            "label": "5h",
                            "scope": "account",
                            "used_percent": 25.0,
                            "remaining_percent": 75.0,
                            "window_seconds": 18000,
                            "resets_at": 1200,
                            "quality": "exact",
                        }
                    ],
                },
                {
                    "provider": "antigravity",
                    "status": "unavailable",
                    "source": "fixture",
                    "observed_at": 1000,
                    "last_observed_at": None,
                    "checked_at": 1000,
                    "buckets": [],
                    "reason": "telemetry_opt_in_required",
                },
            ],
        }


def _serve_once():
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    return server, worker


def test_api_quotas_returns_partial_failure_as_200(monkeypatch):
    service = _FakeQuotaService()
    monkeypatch.setattr(dashboard_server, "QUOTA_SERVICE", service)
    server, worker = _serve_once()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/quotas",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            payload = json.loads(response.read())
    finally:
        worker.join(timeout=6)
        server.server_close()

    assert not worker.is_alive()
    assert service.calls == 1
    assert payload["degraded"] is True
    assert [provider["status"] for provider in payload["providers"]] == [
        "ok",
        "unavailable",
    ]
    assert payload["providers"][0]["buckets"][0]["remaining_percent"] == 75.0


def test_cross_origin_browser_request_is_rejected_before_provider_refresh(monkeypatch):
    service = _FakeQuotaService()
    monkeypatch.setattr(dashboard_server, "QUOTA_SERVICE", service)
    server, worker = _serve_once()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/api/quotas",
        headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
    )
    try:
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert json.loads(exc.read()) == {"error": "cross_origin_quota_request"}
        else:
            raise AssertionError("cross-origin quota request unexpectedly succeeded")
    finally:
        worker.join(timeout=6)
        server.server_close()

    assert not worker.is_alive()
    assert service.calls == 0


def test_non_quota_routes_are_delegated_to_underlying_dashboard():
    server, worker = _serve_once()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/version",
            timeout=5,
        ) as response:
            payload = json.loads(response.read())
    finally:
        worker.join(timeout=6)
        server.server_close()

    assert not worker.is_alive()
    assert payload["name"] == "orrery-telemetry"
    assert payload["api"] == 1


def test_optional_provider_server_still_serves_the_quota_route(monkeypatch):
    """The endpoint is core, so an installed provider payload keeps serving it.

    ``provider_server.py`` loads the core as its own module instance; the quota
    route lives in ``server.py`` instead of in a third entry point so that this
    instance cannot lose it.
    """
    provider_server = importlib.import_module("dashboard.provider_server")
    assert getattr(provider_server, "_GEMINI_PROVIDER_RUNTIME_INSTALLED", False) is True

    service = _FakeQuotaService()
    monkeypatch.setattr(provider_server, "QUOTA_SERVICE", service)
    server = ThreadingHTTPServer(("127.0.0.1", 0), provider_server.Handler)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/quotas", timeout=5
        ) as response:
            assert response.status == 200
            payload = json.loads(response.read())
    finally:
        worker.join(timeout=6)
        server.server_close()

    assert service.calls == 1
    assert [provider["provider"] for provider in payload["providers"]] == [
        "codex",
        "antigravity",
    ]
