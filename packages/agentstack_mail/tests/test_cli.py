from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agentstack_mail import cli


def _settings(
    *,
    host: str = "127.0.0.1",
    port: int = 18765,
    path: str = "/mcp",
    bearer_token: str | None = None,
    jwt_enabled: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        http=SimpleNamespace(
            host=host,
            port=port,
            path=path,
            bearer_token=bearer_token,
            jwt_enabled=jwt_enabled,
        ),
        log_level="INFO",
    )


class _Server:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_main_runs_the_exact_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(path="mcp"))
    monkeypatch.setattr(cli, "build_mcp_server", lambda: server)

    cli.main()

    assert server.calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 18765,
            "path": "/mcp",
            "log_level": "info",
            "json_response": True,
            "stateless_http": True,
            "uvicorn_config": {"loop": "asyncio", "ws": "none"},
        }
    ]


@pytest.mark.parametrize("host", ("0.0.0.0", "mail.local", "192.168.1.10"))
def test_main_rejects_non_loopback_bind(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(host=host))
    monkeypatch.setattr(
        cli,
        "build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="loopback-only"):
        cli.main()


@pytest.mark.parametrize(
    "settings",
    (
        _settings(bearer_token="configured-but-not-wired"),
        _settings(jwt_enabled=True),
    ),
)
def test_main_rejects_unwired_auth_configuration(
    settings: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="authentication is not wired"):
        cli.main()
