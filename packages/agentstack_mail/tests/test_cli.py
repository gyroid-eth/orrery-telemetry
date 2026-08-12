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
        agent_name_enforcement_mode="passthrough",
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
    monkeypatch.setattr(cli, "_build_mcp_server", lambda: server)

    cli.main([])

    assert server.calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 18765,
            "path": "/mcp",
            "log_level": "info",
            "json_response": True,
            "stateless_http": True,
            "uvicorn_config": {
                "loop": "asyncio",
                "ws": "none",
                "timeout_graceful_shutdown": 8.0,
            },
        }
    ]


def test_help_exits_without_loading_settings_or_building_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("help must not load settings")),
    )
    monkeypatch.setattr(
        cli,
        "_build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("help must not build server")),
    )

    with pytest.raises(SystemExit) as exited:
        cli.main(["--help"])

    assert exited.value.code == 0
    output = capsys.readouterr().out
    assert "usage: agentstack-mail" in output
    assert "--host HOST" in output
    assert "--port PORT" in output
    assert "--path PATH" in output


def test_cli_endpoint_arguments_override_environment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: _settings(host="localhost", port=18765, path="/from-env"),
    )
    monkeypatch.setattr(cli, "_build_mcp_server", lambda: server)

    cli.main(["--host", "127.0.0.1", "--port", "18999", "--path", "probe"])

    assert server.calls[0]["host"] == "127.0.0.1"
    assert server.calls[0]["port"] == 18999
    assert server.calls[0]["path"] == "/probe"


@pytest.mark.parametrize("host", ("0.0.0.0", "mail.local", "192.168.1.10"))
def test_main_rejects_non_loopback_bind(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(host=host))
    monkeypatch.setattr(
        cli,
        "_build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="loopback-only"):
        cli.main([])


def test_cli_non_loopback_override_is_rejected_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        cli,
        "_build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="loopback-only"):
        cli.main(["--host", "0.0.0.0"])


def test_cli_rejects_non_passthrough_identity_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.agent_name_enforcement_mode = "coerce"
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "_build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="passthrough is required"):
        cli.main([])


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
        "_build_mcp_server",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before build")),
    )

    with pytest.raises(RuntimeError, match="authentication is not wired"):
        cli.main([])


@pytest.mark.parametrize("arguments", (["--port", "not-an-integer"], ["--unknown"]))
def test_invalid_arguments_fail_before_settings_load(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid arguments must not load settings")
        ),
    )

    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)

    assert exited.value.code == 2
