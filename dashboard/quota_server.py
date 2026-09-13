#!/usr/bin/env python3
"""Dashboard entry point exposing provider account quota telemetry.

The quota layer owns acquisition and GET /api/quotas only. Presentation stays
in the dashboard UI and is intentionally reviewed as a separate change. When an
optional provider-aware server is installed, this wrapper composes on top of it
instead of bypassing its extensions.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from urllib.parse import urlparse

try:
    from dashboard.quotas import build_default_service
except ModuleNotFoundError:  # direct `python dashboard/quota_server.py`
    from quotas import build_default_service


HERE = pathlib.Path(__file__).resolve().parent
QUOTA_SERVICE = build_default_service()


def _load_module(name: str, path: pathlib.Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return sys.modules.get(name, module)


def _load_base():
    provider_server = HERE / "provider_server.py"
    if provider_server.is_file():
        return _load_module("_orrery_quota_provider_base", provider_server)
    return _load_module("_orrery_quota_core_base", HERE / "server.py")


legacy = _load_base()


def _same_origin(handler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    fetch_site = (handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
    expected_origin = f"http://{handler.headers.get('Host', '')}"
    return (not origin or origin == expected_origin) and fetch_site in {
        "",
        "none",
        "same-origin",
    }


class Handler(legacy.Handler):
    def do_GET(self):
        if urlparse(self.path).path != "/api/quotas":
            super().do_GET()
            return

        # A cold read can start an authenticated provider process. Reject a
        # cross-origin browser request before any provider refresh begins.
        if not _same_origin(self):
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
    # The underlying main function resolves Handler from its own module.
    legacy.Handler = Handler
    legacy.main()


if __name__ == "__main__":
    main()
