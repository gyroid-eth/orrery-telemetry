from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import plistlib
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request as urllib_request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client

from agentstack_mail import service


REHEARSAL_LABEL = f"{service.LAUNCHD_REHEARSAL_PREFIX}e6c76c4.a1b2"
LEGACY_BEARER_CANARY = "unchanged-legacy-client-token-canary"


def _assert_secret_absent(secret: str, value: str) -> None:
    if secret in value:
        pytest.fail("secret material was disclosed", pytrace=False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port: int, *, present: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port) is present:
            return
        time.sleep(0.05)
    raise AssertionError(f"port {port} did not reach present={present}")


async def _touch_real_writer(port: int, project: Path) -> None:
    async with Client(f"http://127.0.0.1:{port}/mcp", timeout=5) as client:
        await client.call_tool("ensure_project", {"human_key": str(project)})


def _call_real_mcp_with_legacy_bearer(
    port: int,
    name: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode("utf-8")
    request = urllib_request.Request(
        f"http://127.0.0.1:{port}/api/",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {LEGACY_BEARER_CANARY}",
        },
    )
    with urllib_request.urlopen(request, timeout=5) as response:
        status = response.status
        raw = response.read().decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    envelope = json.loads(raw)
    result = envelope["result"]
    value = result.get("structuredContent")
    if value is None:
        value = next(
            json.loads(block["text"])
            for block in result.get("content", [])
            if block.get("type") == "text"
        )
    return status, envelope, value


def _runtime_environment(env_file: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTSTACK_MAIL_")
        and not key.startswith("MCP_AGENT_MAIL_")
    }
    environment["AGENTSTACK_MAIL_ENV_FILE"] = str(env_file)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _service_command(
    root: Path,
    env_file: Path,
    *,
    server_executable: Path | None = None,
) -> list[str]:
    executable_root = Path(sys.executable).parent
    server_executable = server_executable or executable_root / "agentstack-mail"
    return [
        str(executable_root / "agentstack-mail-service"),
        "foreground",
        "--server-executable",
        str(server_executable),
        "--env-file",
        str(env_file),
        "--state-root",
        str(root),
    ]


def _stop_process_group(process_group: int, *, port: int) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _port_open(port):
            return
        time.sleep(0.05)
    try:
        if _port_open(port):
            os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return


def _pid_recording_server(tmp_path: Path) -> tuple[Path, Path]:
    real_server = Path(sys.executable).parent / "agentstack-mail"
    pid_file = tmp_path / "server.pid"
    shim = tmp_path / "pid-recording-agentstack-mail"
    shim.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                "umask 077",
                f"printf '%s\\n' \"$$\" > {shlex.quote(str(pid_file))}",
                f"exec {shlex.quote(str(real_server))}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim, pid_file


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def _environment(
    path: Path,
    root: Path,
    *,
    mode: str = "passthrough",
    port: int = 18765,
    http_path: str = "/mcp",
    legacy_launchd_label: str | None = None,
    legacy_launchd_receipt: Path | None = None,
    legacy_launchd_receipt_sha256: str | None = None,
) -> Path:
    lines = [
        f"AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE={mode}",
        "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1",
        f"AGENTSTACK_MAIL_HTTP_PORT={port}",
        f"AGENTSTACK_MAIL_HTTP_PATH={http_path}",
        f"AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///{root / 'storage.sqlite3'}",
        f"AGENTSTACK_MAIL_STORAGE_ROOT={root / 'archive'}",
        f"AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR={root / 'signals'}",
    ]
    if legacy_launchd_label is not None:
        lines.append(
            f"AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL={legacy_launchd_label}"
        )
    if legacy_launchd_receipt is not None:
        lines.append(
            f"AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT={legacy_launchd_receipt}"
        )
    if legacy_launchd_receipt_sha256 is not None:
        lines.append(
            "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT_SHA256="
            f"{legacy_launchd_receipt_sha256}"
        )
    path.write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _sealed_legacy_receipt(path: Path, label: str) -> tuple[Path, str]:
    payload = (
        json.dumps(
            {
                "kind": "legacy-launchd-definition",
                "cutover_eligible": True,
                "definition": {
                    "label": label,
                    "state": "loaded",
                    "loaded_path_program_arguments_match_plist": True,
                },
                "runtime": {
                    "listener_port": 8765,
                    "listener_is_wrapper_child": True,
                    "network_requests_sent": 0,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    path.write_bytes(payload)
    path.chmod(0o400)
    return path.resolve(), hashlib.sha256(payload).hexdigest()


def _rendered(
    tmp_path: Path,
    *,
    label: str = service.LAUNCHD_LABEL,
    port: int = 18765,
    http_path: str = "/mcp",
    legacy_launchd_label: str | None = None,
    legacy_receipt_label: str | None = None,
) -> tuple[service.RenderResult, Path, Path]:
    root = (tmp_path / "mail").resolve()
    root.mkdir(parents=True)
    legacy_receipt = None
    legacy_receipt_sha256 = None
    if legacy_launchd_label is not None:
        legacy_receipt, legacy_receipt_sha256 = _sealed_legacy_receipt(
            tmp_path / "legacy-launchd-definition-v1.json",
            legacy_receipt_label or legacy_launchd_label,
        )
    env_file = _environment(
        tmp_path / "mail.env",
        root,
        port=port,
        http_path=http_path,
        legacy_launchd_label=legacy_launchd_label,
        legacy_launchd_receipt=legacy_receipt,
        legacy_launchd_receipt_sha256=legacy_receipt_sha256,
    )
    service_executable = _executable(tmp_path / "agentstack-mail-service")
    server_executable = _executable(tmp_path / "agentstack-mail")
    output = tmp_path / "artifacts"
    result = service.render_launchd(
        output_dir=output,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=root,
        label=label,
    )
    return result, service_executable, server_executable


def test_render_is_pure_content_aware_and_parseable(tmp_path: Path) -> None:
    first, service_executable, server_executable = _rendered(tmp_path)
    artifact = Path(first.artifact)
    ownership = Path(first.ownership_manifest)
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (artifact, ownership)
    }

    second = service.render_launchd(
        output_dir=artifact.parent,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=tmp_path / "mail.env",
        state_root=tmp_path / "mail",
    )

    assert first.status == "rendered"
    assert second.status == "noop"
    assert service.LAUNCHD_LABEL == "org.orrery.mail"
    assert service.LAUNCHD_REHEARSAL_PREFIX == "org.orrery.mail.rehearsal."
    assert artifact.name == service.PLIST_NAME
    assert ownership.name == service.OWNERSHIP_NAME
    assert {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (artifact, ownership)
    } == before
    plist = plistlib.loads(artifact.read_bytes())
    assert plist["Label"] == service.LAUNCHD_LABEL
    assert plist["ProgramArguments"] == [
        str(service_executable),
        "foreground",
        "--server-executable",
        str(server_executable),
        "--env-file",
        str((tmp_path / "mail.env").resolve()),
        "--state-root",
        str((tmp_path / "mail").resolve()),
    ]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 5
    assert set(plist["EnvironmentVariables"]) == {
        "AGENTSTACK_MAIL_ENV_FILE",
        "PATH",
        "LC_ALL",
    }
    combined = artifact.read_text(encoding="utf-8") + ownership.read_text(encoding="utf-8")
    assert ":8765" not in combined
    assert "mcp_agent_mail" not in combined
    assert "mcp-agent-mail" not in combined
    assert stat_mode(artifact) == 0o644
    assert stat_mode(ownership) == 0o600
    assert stat_mode(artifact.parent) == 0o700


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_unexpected_legacy_bearer_header_is_ignored_by_http_entrypoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    port = _free_port()
    env_file = _environment(tmp_path / "mail.env", root, port=port, http_path="/api/")
    server = Path(sys.executable).parent / "agentstack-mail"
    project = tmp_path / "probe-project"
    project.mkdir()
    process = subprocess.Popen(
        [str(server)],
        env=_runtime_environment(env_file),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    server_output = ""
    try:
        _wait_port(port, present=True)
        calls = [
            _call_real_mcp_with_legacy_bearer(
                port,
                "ensure_project",
                {"human_key": str(project)},
            ),
            _call_real_mcp_with_legacy_bearer(
                port,
                "register_agent",
                {
                    "project_key": str(project),
                    "program": "pytest",
                    "model": "fixture",
                    "name": "BlueLake",
                },
            ),
            _call_real_mcp_with_legacy_bearer(port, "health_check", {}),
            _call_real_mcp_with_legacy_bearer(
                port,
                "whois",
                {
                    "project_key": str(project),
                    "agent_name": "BlueLake",
                    "include_recent_commits": False,
                },
            ),
        ]
        assert [status for status, _envelope, _value in calls] == [200] * 4
        for _status, envelope, _value in calls:
            _assert_secret_absent(LEGACY_BEARER_CANARY, json.dumps(envelope))
        health = calls[2][2]
        assert health["status"] == "ok"
        assert health["http_host"] == "127.0.0.1"
        assert health["http_port"] == port
        assert health["database_url"] == (
            f"sqlite+aiosqlite:///{root / 'storage.sqlite3'}"
        )
        whois = calls[3][2]
        assert whois["name"] == "BlueLake"
        assert whois.get("recent_commits", []) == []
    finally:
        if process.poll() is None:
            _stop_process_group(process.pid, port=port)
        stdout, stderr = process.communicate(timeout=15)
        server_output = stdout + stderr
        _wait_port(port, present=False)
    _assert_secret_absent(LEGACY_BEARER_CANARY, server_output)


def test_runtime_preflight_fails_before_artifact(tmp_path: Path) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    env_file = _environment(tmp_path / "mail.env", root, mode="coerce")
    with pytest.raises(service.ServiceError, match="passthrough is required"):
        service.render_launchd(
            output_dir=tmp_path / "artifacts",
            service_executable=_executable(tmp_path / "service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=env_file,
            state_root=root,
        )
    assert not (tmp_path / "artifacts").exists()


def test_runtime_config_allows_same_port_without_manager_io(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label="org.example.legacy-mail",
    )

    ownership = json.loads(Path(rendered.ownership_manifest).read_text(encoding="utf-8"))
    assert ownership["endpoint"] == "http://127.0.0.1:8765/api/"


def test_render_rejects_relative_executable(tmp_path: Path) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    env_file = _environment(tmp_path / "mail.env", root)
    with pytest.raises(service.ServiceError, match="absolute"):
        service.render_launchd(
            output_dir=tmp_path / "artifacts",
            service_executable=Path("agentstack-mail-service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=env_file,
            state_root=root,
        )


class _FakeLaunchctl:
    def __init__(
        self,
        ownership_path: Path,
        *,
        running: bool,
        foreign: bool = False,
        print_returncode: int | None = None,
        fail_operation: str | None = None,
        failed_bootstrap_loads: bool = False,
        bootout_delay_prints: int = 0,
    ) -> None:
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        plist = plistlib.loads(Path(ownership["artifact"]).read_bytes())
        self.artifact = str(Path(ownership["artifact"]).resolve())
        self.arguments = list(plist["ProgramArguments"])
        self.label = str(ownership["label"])
        self.running = running
        self.foreign = foreign
        self.print_returncode = print_returncode
        self.fail_operation = fail_operation
        self.failed_bootstrap_loads = failed_bootstrap_loads
        self.bootout_delay_prints = bootout_delay_prints
        self.remaining_bootout_prints: int | None = None
        self.wrapper_pid = 4242
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        operation = arguments[1]
        if operation == "print":
            if self.remaining_bootout_prints is not None:
                if self.remaining_bootout_prints > 0:
                    self.remaining_bootout_prints -= 1
                else:
                    self.running = False
                    self.remaining_bootout_prints = None
            if self.print_returncode is not None:
                return subprocess.CompletedProcess(
                    arguments, self.print_returncode, "", "manager unavailable"
                )
            if not self.running:
                return subprocess.CompletedProcess(arguments, 113, "", "not found")
            if self.foreign:
                artifact = "/foreign/service.plist"
                program_arguments = ["/foreign/service", "--foreign"]
            else:
                artifact = self.artifact
                program_arguments = self.arguments
            record = "\n".join(
                (
                    f"path = {artifact}",
                    f"program = {program_arguments[0]}",
                    f"pid = {self.wrapper_pid}",
                    "arguments = {",
                    *(f"\t{argument}" for argument in program_arguments),
                    "}",
                    f"note = expected tokens: {self.artifact} {' '.join(self.arguments)}",
                )
            )
            return subprocess.CompletedProcess(arguments, 0, record + "\n", "")
        if operation == self.fail_operation:
            if operation == "bootstrap" and self.failed_bootstrap_loads:
                self.running = True
            return subprocess.CompletedProcess(arguments, 5, "", f"{operation} failed")
        if operation == "bootstrap":
            self.running = True
        elif operation == "bootout":
            if self.bootout_delay_prints:
                self.remaining_bootout_prints = self.bootout_delay_prints
            else:
                self.running = False
        return subprocess.CompletedProcess(arguments, 0, "", "")


def _runner_with_legacy_job(
    fake: _FakeLaunchctl,
    *,
    legacy_label: str,
    loaded: bool,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    identity = f"gui/{os.getuid()}/{legacy_label}"

    def runner(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if arguments[1:] == ["print", identity]:
            fake.calls.append(arguments)
            if loaded:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    "path = /legacy/service.plist\nprogram = /legacy/service\n",
                    "",
                )
            return subprocess.CompletedProcess(arguments, 113, "", "not found")
        return fake(arguments, **kwargs)

    return runner


def _verified_health(
    config: service.RuntimeConfig,
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "status": "verified",
        "endpoint": f"http://{config.host}:{config.port}{config.path}",
        "http_host": config.host,
        "http_port": config.port,
        "database_url": f"sqlite+aiosqlite:///{config.database}",
    }


def test_launchd_pid_parser_rejects_malformed_nonpositive_and_multiple() -> None:
    assert service._parse_launchd_pid("state = running\npid = 42\n") == 42
    assert service._parse_launchd_pid("state = waiting\n") is None
    for output, diagnostic in (
        ("pid = nope\n", "malformed"),
        ("pid = 0\n", "non-positive"),
        ("pid = 41\npid = 42\n", "multiple"),
    ):
        with pytest.raises(service.ServiceError, match=diagnostic):
            service._parse_launchd_pid(output)


def test_lsof_timeout_is_normalized_for_bounded_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["lsof"], 5)

    monkeypatch.setattr(service.subprocess, "run", timeout)

    with pytest.raises(service.ServiceError, match="inspect listeners"):
        service._listener_process_ids(8765)
    with pytest.raises(service.ServiceError, match="inspect parent"):
        service._process_parent_pid(9001)


def test_mcp_health_probe_uses_exact_api_path_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label="org.example.legacy-mail",
    )
    ownership = json.loads(Path(rendered.ownership_manifest).read_text())
    config = service.runtime_config(
        Path(ownership["env_file"]), Path(ownership["state_root"])
    )
    requests: list[Any] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            health = {
                "status": "ok",
                "environment": "test",
                "http_host": "127.0.0.1",
                "http_port": 8765,
                "database_url": f"sqlite+aiosqlite:///{config.database}",
            }
            return json.dumps(
                {"jsonrpc": "2.0", "id": "service-start-readiness", "result": {
                    "isError": False,
                    "structuredContent": health,
                }}
            ).encode()

    def urlopen(request: Any, *, timeout: float) -> Response:
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(service.urllib_request, "urlopen", urlopen)

    result = service._mcp_health_probe(config)

    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:8765/api/"
    assert request.get_method() == "POST"
    assert request.headers["Accept"] == "application/json, text/event-stream"
    assert timeout == 2
    assert result["status"] == "verified"
    assert result["database_url"] == f"sqlite+aiosqlite:///{config.database}"


def test_mcp_health_probe_rejects_wrong_database_without_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label="org.example.legacy-mail",
    )
    ownership = json.loads(Path(rendered.ownership_manifest).read_text())
    config = service.runtime_config(
        Path(ownership["env_file"]), Path(ownership["state_root"])
    )
    requested_urls: list[str] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "service-start-readiness",
                    "result": {
                        "structuredContent": {
                            "status": "ok",
                            "environment": "test",
                            "http_host": "127.0.0.1",
                            "http_port": 8765,
                            "database_url": "sqlite+aiosqlite:////wrong.sqlite3",
                        }
                    },
                }
            ).encode()

    def urlopen(request: Any, *, timeout: float) -> Response:
        requested_urls.append(request.full_url)
        return Response()

    monkeypatch.setattr(service.urllib_request, "urlopen", urlopen)

    with pytest.raises(service.ServiceError, match="expected path and database"):
        service._mcp_health_probe(config)

    assert requested_urls == ["http://127.0.0.1:8765/api/"]


def test_same_port_start_requires_api_and_configured_legacy_label(
    tmp_path: Path,
) -> None:
    rehearsal, _, _ = _rendered(
        tmp_path / "rehearsal",
        label=REHEARSAL_LABEL,
        port=8765,
        http_path="/api/",
        legacy_launchd_label="org.example.legacy-mail",
    )
    rehearsal_fake = _FakeLaunchctl(
        Path(rehearsal.ownership_manifest), running=False
    )
    with pytest.raises(service.ServiceError, match="reserved for the production"):
        service.service_start(
            Path(rehearsal.ownership_manifest),
            label=REHEARSAL_LABEL,
            runner=rehearsal_fake,
        )
    assert rehearsal_fake.calls == []

    wrong_path, _, _ = _rendered(
        tmp_path / "wrong-path",
        port=8765,
        legacy_launchd_label="org.example.legacy-mail",
    )
    wrong_path_fake = _FakeLaunchctl(Path(wrong_path.ownership_manifest), running=False)
    with pytest.raises(service.ServiceError, match="AGENTSTACK_MAIL_HTTP_PATH=/api/"):
        service.service_start(Path(wrong_path.ownership_manifest), runner=wrong_path_fake)
    assert wrong_path_fake.calls == []

    missing_label, _, _ = _rendered(
        tmp_path / "missing-label",
        port=8765,
        http_path="/api/",
    )
    missing_label_fake = _FakeLaunchctl(
        Path(missing_label.ownership_manifest), running=False
    )
    with pytest.raises(
        service.ServiceError,
        match="AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL",
    ):
        service.service_start(
            Path(missing_label.ownership_manifest), runner=missing_label_fake
        )
    assert missing_label_fake.calls == []


def test_same_port_start_rejects_typo_label_bound_to_real_sealed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_label = "org.example.legacy-mail"
    configured_typo = "org.example.legacy-mai"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=configured_typo,
        legacy_receipt_label=actual_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    actual_identity = f"gui/{os.getuid()}/{actual_label}"

    def actual_legacy_is_loaded(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if arguments[1:] == ["print", actual_identity]:
            fake.calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments,
                0,
                "path = /legacy/service.plist\nprogram = /legacy/service\n",
                "",
            )
        return fake(arguments, **kwargs)

    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [],
    )
    precondition = actual_legacy_is_loaded(
        ["launchctl", "print", actual_identity]
    )
    assert precondition.returncode == 0
    fake.calls.clear()

    with pytest.raises(
        service.ServiceError,
        match="configured legacy launchd label does not match the sealed C1 receipt",
    ):
        service.service_start(ownership, runner=actual_legacy_is_loaded)

    assert fake.calls == []


def test_same_port_start_rejects_changed_sealed_receipt_before_launchctl(
    tmp_path: Path,
) -> None:
    label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=label,
    )
    ownership = Path(rendered.ownership_manifest)
    receipt = tmp_path / "legacy-launchd-definition-v1.json"
    receipt.chmod(0o600)
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    receipt.chmod(0o400)
    fake = _FakeLaunchctl(ownership, running=False)

    with pytest.raises(service.ServiceError, match="SHA-256 does not match"):
        service.service_start(ownership, runner=fake)

    assert fake.calls == []


def test_same_port_start_rejects_loaded_legacy_job_before_bootstrap(
    tmp_path: Path,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=True,
    )

    with pytest.raises(service.ServiceError, match="bootout.*legacy"):
        service.service_start(ownership, runner=runner)

    assert [call[1] for call in fake.calls] == ["print", "print"]


def test_same_port_start_rejects_unknown_legacy_job_state_before_listener_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    legacy_identity = f"gui/{os.getuid()}/{legacy_label}"

    def runner(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if arguments[1:] == ["print", legacy_identity]:
            fake.calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments, 5, "", "launchd query unavailable"
            )
        return fake(arguments, **kwargs)

    def unexpected_listener_probe(_port: int) -> list[int]:
        raise AssertionError("listener inspection must follow an exact legacy absence")

    monkeypatch.setattr(service, "_listener_process_ids", unexpected_listener_probe)

    with pytest.raises(service.ServiceError, match="legacy launchd job state is unknown"):
        service.service_start(ownership, runner=runner)

    assert "bootstrap" not in [call[1] for call in fake.calls]


def test_same_port_start_rejects_foreign_listener_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [9001],
    )

    with pytest.raises(service.ServiceError, match="listener.*9001.*bootout"):
        service.service_start(ownership, runner=runner)

    assert "bootstrap" not in [call[1] for call in fake.calls]


def test_same_port_start_runs_only_after_legacy_and_listener_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )
    listener_observations = iter(([], [9001], [9001]))
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: next(listener_observations),
    )
    monkeypatch.setattr(
        service,
        "_process_parent_pid",
        lambda _pid, **_kwargs: fake.wrapper_pid,
    )
    monkeypatch.setattr(service, "_mcp_health_probe", _verified_health)

    started = service.service_start(ownership, runner=runner)

    preflight = started["same_port_preflight"]
    receipt_binding = preflight.pop("legacy_launchd_receipt")
    assert preflight == {
        "status": "accepted",
        "port": 8765,
        "path": "/api/",
        "legacy_launchd_label": legacy_label,
        "legacy_launchd_state": "absent",
        "listener_pids": [],
    }
    assert receipt_binding["status"] == "verified"
    assert receipt_binding["definition_label"] == legacy_label
    assert len(receipt_binding["sha256"]) == 64
    assert started["mcp_readiness"]["status"] == "verified"
    assert started["mcp_readiness"]["listener_observations"] == [
        [9001],
        [9001],
    ]
    assert [call[1] for call in fake.calls][:3] == ["print", "print", "bootstrap"]


def test_same_port_noop_rejects_listener_outside_owned_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=True)
    runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [9002],
    )
    monkeypatch.setattr(
        service,
        "_process_parent_pid",
        lambda _pid, **_kwargs: 9999,
    )

    with pytest.raises(service.ServiceError, match="listener.*9002.*owned launchd job"):
        service.service_start(ownership, runner=runner)

    assert "bootstrap" not in [call[1] for call in fake.calls]


@pytest.mark.parametrize("listeners", [[], [9001, 9002]])
def test_same_port_noop_rejects_zero_or_multiple_owned_listeners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listeners: list[int],
) -> None:
    label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=True)
    runner = _runner_with_legacy_job(fake, legacy_label=label, loaded=False)
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: listeners,
    )

    with pytest.raises(service.ServiceError, match="exactly one listener"):
        service.service_start(ownership, runner=runner)

    assert "bootstrap" not in [call[1] for call in fake.calls]


def test_same_port_noop_accepts_only_listener_child_of_owned_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=True)
    runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [9003],
    )
    monkeypatch.setattr(
        service,
        "_process_parent_pid",
        lambda _pid, **_kwargs: fake.wrapper_pid,
    )
    monkeypatch.setattr(service, "_mcp_health_probe", _verified_health)

    started = service.service_start(ownership, runner=runner)

    assert started["action"] == "noop"
    assert started["same_port_preflight"]["listener_pids"] == [9003]
    assert started["mcp_readiness"]["status"] == "verified"
    assert started["mcp_readiness"]["listener_observations"] == [
        [9003],
        [9003],
    ]
    assert "bootstrap" not in [call[1] for call in fake.calls]


def test_same_port_readiness_does_not_retry_a_legacy_authority_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=label,
    )
    ownership = json.loads(Path(rendered.ownership_manifest).read_text())
    config = service.runtime_config(
        Path(ownership["env_file"]), Path(ownership["state_root"])
    )
    observations = 0

    def conflict(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal observations
        observations += 1
        raise service.HandoffConflictError("legacy authority appeared")

    monkeypatch.setattr(service, "_same_port_runtime_snapshot", conflict)
    monkeypatch.setattr(
        service.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(
            AssertionError("unsafe authority conflicts must not be retried")
        ),
    )

    with pytest.raises(service.HandoffConflictError, match="appeared"):
        service._wait_for_same_port_ready(
            Path(rendered.ownership_manifest),
            config,
            label=service.LAUNCHD_LABEL,
            runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
        )

    assert observations == 1


def test_same_port_readiness_rejects_a_snapshot_completed_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label="org.example.legacy-mail",
    )
    ownership = json.loads(Path(rendered.ownership_manifest).read_text())
    config = service.runtime_config(
        Path(ownership["env_file"]), Path(ownership["state_root"])
    )
    monotonic_values = iter((0.0, 0.0, 21.0))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        service,
        "_same_port_runtime_snapshot",
        lambda *_args, **_kwargs: {"status": "verified"},
    )

    with pytest.raises(service.ServiceError, match="deadline expired"):
        service._wait_for_same_port_ready(
            Path(rendered.ownership_manifest),
            config,
            label=service.LAUNCHD_LABEL,
            runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
            timeout_seconds=20,
        )


def test_same_port_post_bootstrap_readiness_failure_boots_out_new_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    runner = _runner_with_legacy_job(fake, legacy_label=label, loaded=False)
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [],
    )
    monkeypatch.setattr(
        service,
        "_wait_for_same_port_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            service.ServiceError("exact /api/ health never became ready")
        ),
    )

    with pytest.raises(service.ServiceError, match="post-bootstrap verification"):
        service.service_start(ownership, runner=runner)

    operations = [call[1] for call in fake.calls]
    assert "bootstrap" in operations
    assert "bootout" in operations
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False


def test_post_bootstrap_launchctl_timeout_boots_out_owned_new_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    legacy_runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )
    loaded_new_prints = 0

    def timeout_once_after_bootstrap(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal loaded_new_prints
        if arguments[1] == "print" and fake.running:
            loaded_new_prints += 1
            if loaded_new_prints == 2:
                raise subprocess.TimeoutExpired(arguments, 20)
        return legacy_runner(arguments, **kwargs)

    listener_observations = iter(([], [9001]))
    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: next(listener_observations),
    )
    monotonic_values = iter((0.0, 0.0, 0.0, 21.0))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        service,
        "_wait_for_service_stopped",
        lambda *_args, **_kwargs: (
            {"status": "stopped", "owned": True},
            {"poll_count": 1},
        ),
    )

    with pytest.raises(
        service.ServiceError,
        match="post-bootstrap verification.*launchctl print could not complete",
    ):
        service.service_start(ownership, runner=timeout_once_after_bootstrap)

    operations = [call[1] for call in fake.calls]
    assert operations.count("bootstrap") == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False


@pytest.mark.parametrize("operation", ["bootstrap", "enable", "kickstart"])
def test_mutating_launchctl_timeout_reconciles_and_boots_out_exact_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    legacy_label = "org.example.legacy-mail"
    rendered, _, _ = _rendered(
        tmp_path,
        port=8765,
        http_path="/api/",
        legacy_launchd_label=legacy_label,
    )
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    legacy_runner = _runner_with_legacy_job(
        fake,
        legacy_label=legacy_label,
        loaded=False,
    )

    def timeout_after_possible_mutation(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if arguments[1] == operation:
            fake.calls.append(arguments)
            if operation == "bootstrap":
                fake.running = True
            raise subprocess.TimeoutExpired(arguments, 20)
        return legacy_runner(arguments, **kwargs)

    monkeypatch.setattr(
        service,
        "_listener_process_ids",
        lambda _port, **_kwargs: [],
    )
    monkeypatch.setattr(
        service,
        "_wait_for_service_stopped",
        lambda *_args, **_kwargs: (
            {"status": "stopped", "owned": True},
            {"poll_count": 1},
        ),
    )

    with pytest.raises(
        service.ServiceError,
        match=rf"launchctl {operation} outcome is unknown",
    ):
        service.service_start(ownership, runner=timeout_after_possible_mutation)

    operations = [call[1] for call in fake.calls]
    assert operations.count(operation) == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index(operation)
    assert fake.running is False


@pytest.mark.parametrize("operation", ["bootstrap", "enable", "kickstart"])
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_mutating_launchctl_baseexception_reconciles_and_preserves_interrupt(
    tmp_path: Path,
    operation: str,
    exception_type: type[BaseException],
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    interrupted = False

    def interrupt_after_mutation(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal interrupted
        result = fake(arguments, **kwargs)
        if arguments[1] == operation and not interrupted:
            interrupted = True
            raise exception_type()
        return result

    with pytest.raises(exception_type) as raised:
        service.service_start(ownership, runner=interrupt_after_mutation)

    operations = [call[1] for call in fake.calls]
    assert operations.count(operation) == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index(operation)
    assert fake.running is False
    if hasattr(raised.value, "add_note"):
        assert any(
            "compensated and verified stopped" in note
            for note in getattr(raised.value, "__notes__", [])
        )


def test_post_bootstrap_baseexception_compensates_and_preserves_interrupt(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    interrupted = False

    def interrupt_after_final_status_read(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal interrupted
        result = fake(arguments, **kwargs)
        if arguments[1] == "print" and fake.running and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return result

    with pytest.raises(KeyboardInterrupt) as raised:
        service.service_start(ownership, runner=interrupt_after_final_status_read)

    operations = [call[1] for call in fake.calls]
    assert operations.count("bootstrap") == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False
    if hasattr(raised.value, "add_note"):
        assert any(
            "post-bootstrap verification" in note
            and "compensated and verified stopped" in note
            for note in getattr(raised.value, "__notes__", [])
        )


def test_owned_start_and_stop_use_explicit_launchctl_only(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)

    started = service.service_start(ownership, runner=fake)
    stopped = service.service_stop(ownership, runner=fake)

    assert started["status"] == "job_loaded"
    assert started["action"] == "started"
    assert started["bootstrap_preflight"]["launchctl_print_returncode"] == 113
    assert started["bootstrap_eio_recheck"] is None
    assert started["mcp_readiness"] == "unverified"
    assert stopped["status"] == "stopped"
    assert stopped["action"] == "stopped"
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "enable",
        "kickstart",
        "print",
        "print",
        "bootout",
        "print",
    ]
    assert all(call[0] == "launchctl" for call in fake.calls)


def test_rehearsal_label_flows_through_owned_render_start_status_and_stop(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path, label=REHEARSAL_LABEL)
    ownership = Path(rendered.ownership_manifest)
    artifact = Path(rendered.artifact)
    fake = _FakeLaunchctl(ownership, running=False)

    started = service.service_start(ownership, label=REHEARSAL_LABEL, runner=fake)
    status = service.service_status(ownership, label=REHEARSAL_LABEL, runner=fake)
    stopped = service.service_stop(ownership, label=REHEARSAL_LABEL, runner=fake)

    assert artifact.name == f"{REHEARSAL_LABEL}.plist"
    assert ownership.name == f"{REHEARSAL_LABEL}.ownership.json"
    assert plistlib.loads(artifact.read_bytes())["Label"] == REHEARSAL_LABEL
    assert json.loads(ownership.read_text(encoding="utf-8"))["label"] == REHEARSAL_LABEL
    assert started["label"] == status["label"] == stopped["label"] == REHEARSAL_LABEL
    rehearsal_identity = f"gui/{os.getuid()}/{REHEARSAL_LABEL}"
    production_identity = f"gui/{os.getuid()}/{service.LAUNCHD_LABEL}"
    flattened = [argument for call in fake.calls for argument in call]
    assert rehearsal_identity in flattened
    assert production_identity not in flattened


def test_ownership_and_cli_label_mismatch_fails_before_launchctl(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path, label=REHEARSAL_LABEL)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)

    with pytest.raises(service.ServiceError, match="wrong identity"):
        service.service_start(ownership, runner=fake)
    with pytest.raises(service.ServiceError, match="wrong identity"):
        service.service_start(
            ownership,
            label=f"{REHEARSAL_LABEL}.other",
            runner=fake,
        )

    assert fake.calls == []


@pytest.mark.parametrize(
    "label",
    (
        "",
        "com.operator.agentstack-mail-rehearsal",
        service.LAUNCHD_REHEARSAL_PREFIX,
        f"{service.LAUNCHD_REHEARSAL_PREFIX}Uppercase",
        f"{service.LAUNCHD_REHEARSAL_PREFIX}slash/value",
        f"{service.LAUNCHD_REHEARSAL_PREFIX}double..dot",
        f"{service.LAUNCHD_REHEARSAL_PREFIX}trailing-",
        f"{service.LAUNCHD_REHEARSAL_PREFIX}{'a' * 129}",
    ),
)
def test_render_rejects_unreserved_or_malformed_custom_label_before_artifact(
    tmp_path: Path,
    label: str,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    env_file = _environment(tmp_path / "mail.env", root)

    with pytest.raises(service.ServiceError, match="launchd label"):
        service.render_launchd(
            output_dir=tmp_path / "artifacts",
            service_executable=_executable(tmp_path / "service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=env_file,
            state_root=root,
            label=label,
        )

    assert not (tmp_path / "artifacts").exists()


def test_rehearsal_absence_preflight_rejects_production_and_existing_job(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path, label=REHEARSAL_LABEL)
    ownership = Path(rendered.ownership_manifest)
    missing = _FakeLaunchctl(ownership, running=False)
    existing = _FakeLaunchctl(ownership, running=True)

    with pytest.raises(service.ServiceError, match="must not equal the production"):
        service.require_rehearsal_job_absent(service.LAUNCHD_LABEL, runner=missing)
    assert missing.calls == []

    absent = service.require_rehearsal_job_absent(
        REHEARSAL_LABEL,
        runner=missing,
    )
    assert absent == {
        "status": "absent",
        "label": REHEARSAL_LABEL,
        "identity": f"gui/{os.getuid()}/{REHEARSAL_LABEL}",
    }
    with pytest.raises(service.ServiceError, match="already exists"):
        service.require_rehearsal_job_absent(REHEARSAL_LABEL, runner=existing)
    assert [call[1] for call in existing.calls] == ["print"]


def test_cli_parser_defaults_production_and_accepts_explicit_rehearsal_label() -> None:
    default = service._parser().parse_args(
        ["status", "--ownership-manifest", "/tmp/ownership.json"]
    )
    rehearsal = service._parser().parse_args(
        [
            "status",
            "--ownership-manifest",
            "/tmp/ownership.json",
            "--label",
            REHEARSAL_LABEL,
        ]
    )

    assert default.label == service.LAUNCHD_LABEL
    assert rehearsal.label == REHEARSAL_LABEL


def test_stop_refuses_foreign_job_without_bootout(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=True, foreign=True)

    with pytest.raises(service.ServiceError, match="foreign or unknown"):
        service.service_stop(ownership, runner=fake)

    assert [call[1] for call in fake.calls] == ["print"]


def test_changed_artifact_invalidates_ownership_before_manager_call(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=True)
    Path(rendered.artifact).write_text("changed", encoding="utf-8")

    with pytest.raises(service.ServiceError, match="digest has changed"):
        service.service_stop(ownership, runner=fake)

    assert fake.calls == []


def test_changed_environment_invalidates_ownership_before_manager_call(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    env_file = tmp_path / "mail.env"
    env_file.write_text(env_file.read_text() + "# changed after render\n", encoding="utf-8")
    fake = _FakeLaunchctl(ownership, running=True)

    with pytest.raises(service.ServiceError, match="changed after render"):
        service.service_start(ownership, runner=fake)

    assert fake.calls == []


def test_environment_drift_is_reported_but_does_not_block_owned_stop(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    (tmp_path / "mail.env").unlink()
    fake = _FakeLaunchctl(ownership, running=True)

    status = service.service_status(ownership, runner=fake)
    stopped = service.service_stop(ownership, runner=fake)

    assert status["status"] == "job_loaded"
    assert status["environment_drift"] is True
    assert stopped["status"] == "stopped"
    assert "bootout" in [call[1] for call in fake.calls]


def test_manager_query_error_is_unknown_and_never_treated_as_stopped(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False, print_returncode=5)

    with pytest.raises(service.ServiceError, match="job state is unknown"):
        service.service_start(ownership, runner=fake)

    assert [call[1] for call in fake.calls] == ["print"]


def test_generic_not_found_text_is_not_a_service_absence(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)

    def failed_query(
        arguments: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments, 5, "", "launchctl manager helper not found"
        )

    with pytest.raises(service.ServiceError, match="job state is unknown"):
        service.service_start(ownership, runner=failed_query)


def test_partial_start_is_compensated_to_stopped(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False, fail_operation="enable")

    with pytest.raises(service.ServiceError, match="compensated and verified stopped"):
        service.service_start(ownership, runner=fake)

    assert fake.running is False
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "enable",
        "print",
        "bootout",
        "print",
    ]


def test_bootstrap_eio_is_reconciled_only_when_exact_job_is_loaded(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
        failed_bootstrap_loads=True,
    )

    started = service.service_start(ownership, runner=fake)

    assert started["status"] == "job_loaded"
    assert started["action"] == "started"
    assert started["bootstrap_outcome"] == "exact_job_already_loaded_after_eio"
    assert started["bootstrap_preflight"]["launchctl_print_returncode"] == 113
    assert started["bootstrap_eio_recheck"]["launchctl_print_returncode"] == 0
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "print",
        "enable",
        "kickstart",
        "print",
    ]
    assert [call[1] for call in fake.calls].count("bootstrap") == 1


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_bootstrap_eio_recheck_baseexception_compensates_loaded_job(
    tmp_path: Path,
    exception_type: type[BaseException],
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
        failed_bootstrap_loads=True,
    )
    interrupted = False

    def interrupt_eio_recheck_after_observation(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal interrupted
        result = fake(arguments, **kwargs)
        if arguments[1] == "print" and fake.running and not interrupted:
            interrupted = True
            raise exception_type()
        return result

    with pytest.raises(exception_type) as raised:
        service.service_start(ownership, runner=interrupt_eio_recheck_after_observation)

    operations = [call[1] for call in fake.calls]
    assert operations.count("bootstrap") == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False
    if hasattr(raised.value, "add_note"):
        assert any(
            "launchctl bootstrap outcome is unknown" in note
            and "compensated and verified stopped" in note
            for note in getattr(raised.value, "__notes__", [])
        )


def test_bootstrap_nonzero_after_mutation_compensates_loaded_job(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)

    def nonzero_after_bootstrap_mutation(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        result = fake(arguments, **kwargs)
        if arguments[1] == "bootstrap":
            return subprocess.CompletedProcess(
                arguments,
                64,
                result.stdout,
                "bootstrap returned an application error",
            )
        return result

    with pytest.raises(
        service.ServiceError,
        match="bootstrap returned an application error.*compensated and verified stopped",
    ):
        service.service_start(ownership, runner=nonzero_after_bootstrap_mutation)

    operations = [call[1] for call in fake.calls]
    assert operations.count("bootstrap") == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_bootstrap_result_inspection_baseexception_compensates_loaded_job(
    tmp_path: Path,
    exception_type: type[BaseException],
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)

    class InterruptedResult:
        @property
        def returncode(self) -> int:
            raise exception_type()

    def interrupt_result_inspection(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str] | InterruptedResult:
        result = fake(arguments, **kwargs)
        if arguments[1] == "bootstrap":
            return InterruptedResult()
        return result

    with pytest.raises(exception_type) as raised:
        service.service_start(ownership, runner=interrupt_result_inspection)

    operations = [call[1] for call in fake.calls]
    assert operations.count("bootstrap") == 1
    assert operations.count("bootout") == 1
    assert operations.index("bootout") > operations.index("bootstrap")
    assert fake.running is False
    if hasattr(raised.value, "add_note"):
        assert any(
            "launchctl bootstrap outcome is unknown" in note
            and "compensated and verified stopped" in note
            for note in getattr(raised.value, "__notes__", [])
        )


def test_bootstrap_eio_with_absent_job_fails_without_more_mutation(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
    )

    with pytest.raises(service.ServiceError, match="exact label remains absent"):
        service.service_start(ownership, runner=fake)

    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "print",
        "print",
    ]


def test_bootstrap_eio_with_foreign_job_fails_without_more_mutation(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
        failed_bootstrap_loads=True,
    )

    def foreign_after_bootstrap(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        result = fake(arguments, **kwargs)
        if arguments[1] == "bootstrap":
            fake.foreign = True
        return result

    with pytest.raises(
        service.ServiceError,
        match="loaded label is foreign or unknown.*no bootout attempted",
    ):
        service.service_start(ownership, runner=foreign_after_bootstrap)

    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "print",
        "print",
    ]
    assert not {"enable", "kickstart", "bootout"}.intersection(
        call[1] for call in fake.calls
    )


def test_bootstrap_eio_with_unknown_job_fails_without_more_mutation(
    tmp_path: Path,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
        failed_bootstrap_loads=True,
    )

    def unknown_after_bootstrap(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        result = fake(arguments, **kwargs)
        if arguments[1] == "bootstrap":
            fake.print_returncode = 5
        return result

    with pytest.raises(
        service.ServiceError,
        match="job state is unknown.*could not be inspected",
    ):
        service.service_start(ownership, runner=unknown_after_bootstrap)

    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "print",
        "print",
    ]
    assert not {"enable", "kickstart", "bootout"}.intersection(
        call[1] for call in fake.calls
    )


def test_bootstrap_eio_with_environment_drift_is_compensated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=False,
        fail_operation="bootstrap",
        failed_bootstrap_loads=True,
    )
    drift = iter((False, False, True, True, True))
    monkeypatch.setattr(service, "_environment_drift", lambda _ownership: next(drift))

    with pytest.raises(service.ServiceError, match="environment drift"):
        service.service_start(ownership, runner=fake)

    assert fake.running is False
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "print",
        "print",
        "bootout",
        "print",
    ]


def test_final_started_state_with_environment_drift_is_compensated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    drift = iter((False, False, True, True, True))
    monkeypatch.setattr(service, "_environment_drift", lambda _ownership: next(drift))

    with pytest.raises(service.ServiceError, match="environment drift"):
        service.service_start(ownership, runner=fake)

    assert fake.running is False
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootstrap",
        "enable",
        "kickstart",
        "print",
        "print",
        "bootout",
        "print",
    ]


def test_service_stop_waits_for_asynchronous_bootout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=True,
        bootout_delay_prints=2,
    )
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)

    stopped = service.service_stop(ownership, runner=fake)

    assert stopped["status"] == "stopped"
    assert stopped["stop_wait"]["poll_count"] == 3
    assert [call[1] for call in fake.calls] == [
        "print",
        "bootout",
        "print",
        "print",
        "print",
    ]


def test_service_stop_rejects_ownership_change_during_bootout_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(
        ownership,
        running=True,
        bootout_delay_prints=10,
    )
    bootout_seen = False
    poll_count = 0

    def foreign_during_poll(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal bootout_seen, poll_count
        operation = arguments[1]
        if operation == "bootout":
            bootout_seen = True
        elif operation == "print" and bootout_seen:
            poll_count += 1
            if poll_count == 2:
                fake.foreign = True
        return fake(arguments, **kwargs)

    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    with pytest.raises(service.ServiceError, match="changed ownership"):
        service.service_stop(ownership, runner=foreign_during_poll)

    assert [call[1] for call in fake.calls] == [
        "print",
        "bootout",
        "print",
        "print",
    ]
    assert sum(call[1] == "bootout" for call in fake.calls) == 1


def test_service_stop_rejects_stopped_observation_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    observed = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(observed))

    with pytest.raises(service.ServiceError, match="stop deadline"):
        service._wait_for_service_stopped(
            ownership,
            label=service.LAUNCHD_LABEL,
            runner=fake,
            timeout_seconds=30,
        )

    assert [call[1] for call in fake.calls] == ["print"]


def test_default_runner_is_resolved_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)
    monkeypatch.setattr(service.subprocess, "run", fake)

    assert service.service_status(ownership)["status"] == "stopped"
    assert [call[1] for call in fake.calls] == ["print"]


def test_render_repairs_directory_mode_and_reports_the_change(tmp_path: Path) -> None:
    rendered, service_executable, server_executable = _rendered(tmp_path)
    output = Path(rendered.artifact).parent
    artifact = Path(rendered.artifact)
    ownership = Path(rendered.ownership_manifest)
    output.chmod(0o755)
    artifact.chmod(0o600)
    ownership.chmod(0o644)

    repaired = service.render_launchd(
        output_dir=output,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=tmp_path / "mail.env",
        state_root=tmp_path / "mail",
    )
    stable = service.render_launchd(
        output_dir=output,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=tmp_path / "mail.env",
        state_root=tmp_path / "mail",
    )

    assert repaired.status == "rendered"
    assert stat_mode(output) == 0o700
    assert stat_mode(artifact) == 0o644
    assert stat_mode(ownership) == 0o600
    assert stable.status == "noop"


def test_render_rejects_live_launchagents_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "mail"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env_file = _environment(tmp_path / "mail.env", root)

    with pytest.raises(service.ServiceError, match="not live LaunchAgents"):
        service.render_launchd(
            output_dir=home / "Library" / "LaunchAgents" / "staging",
            service_executable=_executable(tmp_path / "service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=env_file,
            state_root=root,
        )


def test_runtime_rejects_state_root_nested_below_legacy_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / "mcp_agent_mail" / "nested-new-root"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    env_file = _environment(tmp_path / "mail.env", root)

    with pytest.raises(service.ServiceError, match="overlap with a legacy"):
        service.runtime_config(env_file, root)


def test_real_user_home_guards_survive_scratch_home_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_home = tmp_path / "scratch-home"
    real_home = tmp_path / "real-home"
    safe_root = tmp_path / "mail"
    scratch_home.mkdir()
    safe_root.mkdir()
    monkeypatch.setenv("HOME", str(scratch_home))
    monkeypatch.setattr(
        service.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(real_home)),
    )

    legacy_nested = real_home / "mcp_agent_mail" / "nested-new-root"
    legacy_env = _environment(tmp_path / "legacy.env", legacy_nested)
    with pytest.raises(service.ServiceError, match="overlap with a legacy"):
        service.runtime_config(legacy_env, legacy_nested)

    safe_env = _environment(tmp_path / "safe.env", safe_root)
    with pytest.raises(service.ServiceError, match="not live LaunchAgents"):
        service.render_launchd(
            output_dir=real_home / "Library" / "LaunchAgents" / "staging",
            service_executable=_executable(tmp_path / "service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=safe_env,
            state_root=safe_root,
        )


def test_render_rejects_symlink_artifact_instead_of_reporting_noop(
    tmp_path: Path,
) -> None:
    rendered, service_executable, server_executable = _rendered(tmp_path)
    artifact = Path(rendered.artifact)
    target = tmp_path / "foreign"
    target.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(target)

    with pytest.raises(service.ServiceError, match="non-regular artifact"):
        service.render_launchd(
            output_dir=artifact.parent,
            service_executable=service_executable,
            server_executable=server_executable,
            env_file=tmp_path / "mail.env",
            state_root=tmp_path / "mail",
        )


def test_foreground_lock_rejects_second_writer_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "archive").mkdir()
    (root / "signals").mkdir()
    (root / "storage.sqlite3").touch()
    runtime = root / "runtime"
    runtime.mkdir()
    env_file = _environment(tmp_path / "mail.env", root)
    server = _executable(tmp_path / "server")
    lock = (runtime / "authority.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("server must not spawn")
        ),
    )
    try:
        with pytest.raises(service.ServiceError, match="another service process"):
            service.foreground(
                server_executable=server,
                env_file=env_file,
                state_root=root,
            )
    finally:
        lock.close()


def test_foreground_rejects_unconfirmed_migration_publication_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "archive").mkdir()
    (root / "signals").mkdir()
    (root / "storage.sqlite3").touch()
    (root / service.MIGRATION_STAGING_MARKER).write_text("{}", encoding="utf-8")
    env_file = _environment(tmp_path / "mail.env", root)
    server = _executable(tmp_path / "server")
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("server must not spawn")
        ),
    )

    with pytest.raises(service.ServiceError, match="publication is unconfirmed"):
        service.foreground(
            server_executable=server,
            env_file=env_file,
            state_root=root,
        )


def test_foreground_starts_server_in_an_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "archive").mkdir()
    (root / "signals").mkdir()
    (root / "storage.sqlite3").touch()
    env_file = _environment(tmp_path / "mail.env", root)
    server = _executable(tmp_path / "server")
    captured: dict[str, Any] = {}

    class ExitedProcess:
        pid = 424242
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            captured.setdefault("wait_timeouts", []).append(timeout)
            return 0

        def poll(self) -> int:
            return 0

    def fake_popen(arguments: list[str], **kwargs: Any) -> ExitedProcess:
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        captured["passed_fd_modes"] = [
            stat.S_IFMT(os.fstat(descriptor).st_mode)
            for descriptor in kwargs["pass_fds"]
        ]
        return ExitedProcess()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "_process_group_exists", lambda _pid: False)

    assert service.foreground(
        server_executable=server,
        env_file=env_file,
        state_root=root,
    ) == 0
    assert captured["arguments"] == [str(server)]
    assert captured["kwargs"]["start_new_session"] is True
    assert len(captured["kwargs"]["pass_fds"]) == 1
    assert captured["passed_fd_modes"] == [stat.S_IFREG]


def test_wrapper_sigkill_leaves_child_lock_blocking_a_replacement_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "archive").mkdir()
    (root / "signals").mkdir()
    (root / "storage.sqlite3").touch()
    project = tmp_path / "project"
    project.mkdir()
    port = _free_port()
    env_file = _environment(tmp_path / "mail.env", root, port=port)
    server_shim, child_pid_file = _pid_recording_server(tmp_path)
    command = _service_command(root, env_file, server_executable=server_shim)
    wrapper = subprocess.Popen(
        command,
        env=_runtime_environment(env_file),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    replacement: subprocess.Popen[str] | None = None
    try:
        _wait_port(port, present=True)
        asyncio.run(_touch_real_writer(port, project))
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        assert child_pid != wrapper.pid

        wrapper.kill()
        assert wrapper.wait(timeout=5) == -signal.SIGKILL
        _wait_port(port, present=True)

        replacement = subprocess.Popen(
            command,
            env=_runtime_environment(env_file),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, replacement_stderr = replacement.communicate(timeout=5)

        assert replacement.returncode == 1
        assert "another service process owns this state root" in replacement_stderr
        assert int(child_pid_file.read_text(encoding="utf-8").strip()) == child_pid
        asyncio.run(_touch_real_writer(port, project))
    finally:
        if replacement is not None and replacement.poll() is None:
            replacement.kill()
            replacement.wait(timeout=5)
        if child_pid is not None:
            _stop_process_group(child_pid, port=port)
        _wait_port(port, present=False)


def test_missing_lock_inheritance_mutant_allows_two_real_writers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "archive").mkdir()
    (root / "signals").mkdir()
    (root / "storage.sqlite3").touch()
    project = tmp_path / "project"
    project.mkdir()
    first_port = _free_port()
    second_port = _free_port()
    first_env = _environment(tmp_path / "first.env", root, port=first_port)
    second_env = _environment(tmp_path / "second.env", root, port=second_port)
    lock_path = root / "runtime" / "authority.lock"
    lock_path.parent.mkdir()
    lock_handle = lock_path.open("a+")
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = Path(sys.executable).parent / "agentstack-mail"
    # This is the exact original mutant: the wrapper owns the lock but does not
    # pass its descriptor to the real server child.
    first = subprocess.Popen(
        [str(server)],
        env=_runtime_environment(first_env),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        _wait_port(first_port, present=True)
        asyncio.run(_touch_real_writer(first_port, project))
        lock_handle.close()

        second = subprocess.Popen(
            _service_command(root, second_env),
            env=_runtime_environment(second_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_port(second_port, present=True)
        asyncio.run(_touch_real_writer(second_port, project))

        assert _port_open(first_port)
        assert _port_open(second_port)
        assert first.poll() is None
        assert second.poll() is None
    finally:
        if not lock_handle.closed:
            lock_handle.close()
        if second is not None and second.poll() is None:
            second.send_signal(signal.SIGTERM)
            second.wait(timeout=15)
        if first.poll() is None:
            _stop_process_group(first.pid, port=first_port)
        _wait_port(first_port, present=False)
        _wait_port(second_port, present=False)


def test_process_group_shutdown_escalates_and_reaps_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []

    class HungProcess:
        pid = 515151
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("server", timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

    monkeypatch.setattr(service, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        service,
        "_signal_process_group",
        lambda pid, signum: signals.append((pid, signum)),
    )
    process = HungProcess()

    service._terminate_process_group(process, grace_seconds=0.01)  # type: ignore[arg-type]

    assert signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.returncode == -signal.SIGKILL


def test_process_group_shutdown_does_not_repeat_forwarded_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []

    class HungProcess:
        pid = 616161
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("server", timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

    monkeypatch.setattr(service, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        service,
        "_signal_process_group",
        lambda pid, signum: signals.append((pid, signum)),
    )
    process = HungProcess()

    service._terminate_process_group(  # type: ignore[arg-type]
        process,
        grace_seconds=0.01,
        term_already_sent=True,
    )

    assert signals == [(process.pid, signal.SIGKILL)]
    assert process.returncode == -signal.SIGKILL


def test_ownership_manifest_does_not_copy_dotenv_secrets(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    env_file = tmp_path / "mail.env"
    env_file.write_text(env_file.read_text() + "AGENTSTACK_MAIL_BEARER_TOKEN=secret-value\n")
    payload = Path(rendered.ownership_manifest).read_text(encoding="utf-8")
    assert "secret-value" not in payload
    assert "BEARER_TOKEN" not in payload
    assert json.loads(payload)["endpoint"] == "http://127.0.0.1:18765/mcp"
