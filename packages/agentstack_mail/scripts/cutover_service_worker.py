#!/usr/bin/env python3
"""Run one frozen-live or Core MCP service for the hermetic cutover gates."""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path


def _install_disabled_llm(namespace: str) -> None:
    module = types.ModuleType(f"{namespace}.llm")

    async def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cutover service worker entered the disabled LLM seam")

    module.complete_system_user = fail_if_called  # type: ignore[attr-defined]
    sys.modules[f"{namespace}.llm"] = module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args()

    source_root = args.source_root.resolve(strict=True)
    if args.host != "127.0.0.1":
        raise RuntimeError("cutover service worker is loopback-only")
    if args.port in {7333, 8765, 8770}:
        raise RuntimeError("cutover service worker refuses a production-reserved port")

    _install_disabled_llm(args.namespace)
    app = importlib.import_module(f"{args.namespace}.app")
    Path(app.__file__).resolve(strict=True).relative_to(source_root)
    server = app.build_mcp_server()
    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        path=args.path,
        log_level="warning",
        json_response=True,
        stateless_http=True,
        uvicorn_config={
            "loop": "asyncio",
            "ws": "none",
            "timeout_graceful_shutdown": 8.0,
        },
    )


if __name__ == "__main__":
    main()
