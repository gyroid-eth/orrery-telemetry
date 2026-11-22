"""Guard ordering without opening a socket or invoking a quota provider."""
from types import SimpleNamespace

import pytest

import _dashboard_http_fixture as fixture


def test_pytest_defaults_replace_both_dashboard_core_instances():
    from dashboard import server, provider_server
    fixture.require_offline_quotas(server)
    fixture.require_offline_quotas(provider_server)


def test_missing_replacement_is_rejected_before_service_read_or_bind(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('real quota service or HTTP bind was called')
    dashboard = SimpleNamespace(QUOTA_SERVICE=SimpleNamespace(read_all=forbidden))
    monkeypatch.setattr(fixture, 'ThreadingHTTPServer', forbidden)
    with pytest.raises(RuntimeError, match='before HTTP bind'):
        fixture.dashboard_http_fixture(dashboard)


def test_installed_offline_service_allows_bind_with_empty_quotas(monkeypatch):
    dashboard = SimpleNamespace(Handler=object())
    service = fixture.install_offline_quotas(dashboard)
    calls = []
    monkeypatch.setattr(fixture, 'ThreadingHTTPServer', lambda address, handler: calls.append((address, handler)))
    fixture.dashboard_http_fixture(dashboard)
    assert calls == [(('127.0.0.1', 0), dashboard.Handler)]
    assert service.read_all() == {'providers': [], 'ts': 0, 'fixture': True}
