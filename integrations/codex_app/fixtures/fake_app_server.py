#!/usr/bin/env python3
"""Deterministic JSON-RPC echo server used by protocol unit tests."""

from __future__ import annotations

import json
import sys


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    initialized = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if "id" not in message:
            if method == "initialized":
                initialized = True
            continue
        request_id = message["id"]
        params = message.get("params", {})
        if method == "test/error":
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": "expected fake failure"},
                }
            )
            continue
        if method == "initialize":
            result = {"serverInfo": {"name": "fake-app-server", "version": "1"}}
        elif method == "thread/list":
            result = {
                "data": [],
                "nextCursor": None,
                "initialized": initialized,
                "method": method,
                "params": params,
            }
        elif method == "thread/start":
            emit(
                {
                    "jsonrpc": "2.0",
                    "method": "thread/started",
                    "params": {"thread": {"id": "thread-example"}},
                }
            )
            result = {
                "thread": {"id": "thread-example", "cwd": params.get("cwd")},
                "method": method,
                "params": params,
            }
        elif method == "turn/start":
            result = {
                "turn": {"id": "turn-example", "status": "inProgress"},
                "method": method,
                "params": params,
            }
        elif method == "thread/inject_items":
            result = {"method": method, "params": params}
        else:
            result = {"method": method, "params": params}
        emit({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
