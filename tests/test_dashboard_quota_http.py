from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

import dashboard.quota_server as quota_server
from dashboard import service_runner


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
    server = ThreadingHTTPServer(("127.0.0.1", 0), quota_server.Handler)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    return server, worker


def test_api_quotas_returns_partial_failure_as_200(monkeypatch):
    service = _FakeQuotaService()
    monkeypatch.setattr(quota_server, "QUOTA_SERVICE", service)
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
    monkeypatch.setattr(quota_server, "QUOTA_SERVICE", service)
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


def test_quota_entrypoint_composes_with_optional_provider_server_when_present():
    provider_server = quota_server.HERE / "provider_server.py"
    assert provider_server.is_file()
    assert getattr(quota_server.legacy, "_GEMINI_PROVIDER_RUNTIME_INSTALLED", False) is True


def test_service_runner_prefers_quota_entrypoint():
    selected = service_runner._default_server_path()
    assert selected.name == "quota_server.py"
    assert selected.is_file()
