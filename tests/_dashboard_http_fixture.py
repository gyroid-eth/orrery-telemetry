"""Offline quota binding for dashboard HTTP/browser fixtures.

Temporary HOME does not isolate OS credential stores. Install this replacement
before opening HTTP, then use the guarded factory. Product code is unchanged.
"""
from http.server import ThreadingHTTPServer


class OfflineQuotaService:
    def read_all(self):
        return {'providers': [], 'ts': 0, 'fixture': True}


def install_offline_quotas(dashboard, *, monkeypatch=None):
    service = OfflineQuotaService()
    if monkeypatch is None:
        dashboard.QUOTA_SERVICE = service
    else:
        monkeypatch.setattr(dashboard, 'QUOTA_SERVICE', service)
    return service


def require_offline_quotas(dashboard):
    service = getattr(dashboard, 'QUOTA_SERVICE', None)
    # Check the type before calling anything: never probe a real quota service.
    if (type(service) is not OfflineQuotaService
            or service.read_all() != {'providers': [], 'ts': 0, 'fixture': True}):
        raise RuntimeError('Dashboard fixture requires offline quotas before HTTP bind')


def dashboard_http_fixture(dashboard, address=('127.0.0.1', 0), *, handler=None):
    require_offline_quotas(dashboard)
    return ThreadingHTTPServer(address, handler or dashboard.Handler)
