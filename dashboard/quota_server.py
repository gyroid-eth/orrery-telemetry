#!/usr/bin/env python3
"""Dashboard server wrapper exposing provider account quota telemetry.

This module deliberately adds only GET /api/quotas. UI rendering stays in the
regular dashboard and can be reviewed separately.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

try:
    from dashboard import server as legacy
    from dashboard.quotas import build_default_service
except ModuleNotFoundError:  # direct `python dashboard/quota_server.py`
    import server as legacy
    from quotas import build_default_service


QUOTA_SERVICE = build_default_service()


class Handler(legacy.Handler):
    def do_GET(self):
        if urlparse(self.path).path != "/api/quotas":
            super().do_GET()
            return

        # A cache miss can start an authenticated provider process. Reject
        # cross-origin browser triggers before any provider refresh begins.
        origin = (self.headers.get("Origin") or "").strip()
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        expected_origin = f"http://{self.headers.get('Host', '')}"
        if ((origin and origin != expected_origin)
                or fetch_site not in {"", "none", "same-origin"}):
            self._send(
                403,
                b'{"error":"cross_origin_quota_request"}',
                "application/json; charset=utf-8",
            )
            return

        body = json.dumps(
            QUOTA_SERVICE.read_all(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")


def main() -> None:
    # legacy.main resolves Handler from its own module at runtime. Replacing the
    # reference keeps existing cleanup, watchdog, routes, and actions intact.
    legacy.Handler = Handler
    legacy.main()


if __name__ == "__main__":
    main()
