from __future__ import annotations

import asyncio
import signal

import pytest
import uvicorn
from fastmcp import FastMCP

from agentstack_mail import db
from agentstack_mail.boundary import (
    CompatibilityFastMCP,
    _AgentStackUvicornServer,
)


def test_database_shutdown_disposes_engine_and_clears_process_globals(
    monkeypatch,
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_session_factory", object())
    monkeypatch.setattr(db, "_schema_ready", True)
    monkeypatch.setattr(db, "_schema_lock", object())

    asyncio.run(db.dispose_database_for_shutdown())

    assert engine.disposed is True
    assert db._engine is None
    assert db._session_factory is None
    assert db._schema_ready is False
    assert db._schema_lock is None


def test_uvicorn_signal_capture_suppresses_only_sigterm(
    monkeypatch,
) -> None:
    server = object.__new__(_AgentStackUvicornServer)
    server._captured_signals = [signal.SIGTERM, signal.SIGINT]
    raised: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda *_args: signal.SIG_DFL)
    monkeypatch.setattr(signal, "raise_signal", raised.append)

    with server.capture_signals():
        pass

    assert raised == [signal.SIGINT]


def test_compatibility_server_scopes_uvicorn_override(
    monkeypatch,
) -> None:
    original_server = uvicorn.Server
    observed: list[type[uvicorn.Server]] = []

    async def fake_run_http_async(_server, *_args, **_kwargs) -> None:
        observed.append(uvicorn.Server)

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)
    server = CompatibilityFastMCP(name="lifecycle-test")

    asyncio.run(server.run_http_async())

    assert observed == [_AgentStackUvicornServer]
    assert uvicorn.Server is original_server


def test_unexpected_uvicorn_version_fails_before_server_start(
    monkeypatch,
) -> None:
    started = False

    async def fake_run_http_async(_server, *_args, **_kwargs) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(uvicorn, "__version__", "0.53.0")
    server = CompatibilityFastMCP(name="lifecycle-version-mutation")

    with pytest.raises(RuntimeError, match="SIGTERM re-raise suppression is pinned"):
        asyncio.run(server.run_http_async())

    assert started is False
