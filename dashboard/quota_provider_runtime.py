"""Add provider account quota telemetry to the provider-aware Dashboard.

This optional extension owns acquisition and GET /api/quotas only. It does not
patch dashboard HTML or otherwise define presentation policy; UI is reviewed as
a separate change.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

try:
    from dashboard.quotas import build_default_service
except ModuleNotFoundError:  # installed dashboard directory on sys.path
    from quotas import build_default_service


_SERVICE = build_default_service()


def _same_origin(self: Any) -> bool:
    origin = (self.headers.get("Origin") or "").strip()
    fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
    expected_origin = f"http://{self.headers.get('Host', '')}"
    return (not origin or origin == expected_origin) and fetch_site in {
        "",
        "none",
        "same-origin",
    }


def install(base: Any) -> Any:
    if getattr(base, "_QUOTA_PROVIDER_RUNTIME_INSTALLED", False):
        return base
    if not hasattr(base, "Handler"):
        raise RuntimeError("dashboard core lacks HTTP Handler")

    original = base.Handler.do_GET

    def do_GET(self: Any) -> None:
        if urlparse(self.path).path != "/api/quotas":
            original(self)
            return
        # A cold read may spawn an authenticated provider process. Reject
        # cross-origin browser triggers before acquisition starts.
        if not _same_origin(self):
            self._send(
                403,
                b'{"error":"cross_origin_quota_request"}',
                "application/json; charset=utf-8",
            )
            return
        body = json.dumps(
            _SERVICE.read_all(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    base.Handler.do_GET = do_GET
    base._QUOTA_PROVIDER_RUNTIME_INSTALLED = True
    base._QUOTA_SERVICE = _SERVICE
    return base
