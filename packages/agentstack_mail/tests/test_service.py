from __future__ import annotations

import fcntl
import json
import os
import plistlib
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentstack_mail import service


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def _environment(path: Path, root: Path, *, mode: str = "passthrough", port: int = 18765) -> Path:
    path.write_text(
        "\n".join(
            (
                f"AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE={mode}",
                "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1",
                f"AGENTSTACK_MAIL_HTTP_PORT={port}",
                "AGENTSTACK_MAIL_HTTP_PATH=/mcp",
                f"AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///{root / 'storage.sqlite3'}",
                f"AGENTSTACK_MAIL_STORAGE_ROOT={root / 'archive'}",
                f"AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR={root / 'signals'}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _rendered(tmp_path: Path) -> tuple[service.RenderResult, Path, Path]:
    root = (tmp_path / "mail").resolve()
    root.mkdir()
    env_file = _environment(tmp_path / "mail.env", root)
    service_executable = _executable(tmp_path / "agentstack-mail-service")
    server_executable = _executable(tmp_path / "agentstack-mail")
    output = tmp_path / "artifacts"
    result = service.render_launchd(
        output_dir=output,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=root,
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
    assert {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (artifact, ownership)
    } == before
    plist = plistlib.loads(artifact.read_bytes())
    assert plist["Label"] == "org.agentstack.mail"
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


@pytest.mark.parametrize(
    ("mode", "port", "message"),
    (
        ("coerce", 18765, "passthrough is required"),
        ("passthrough", 8765, "legacy AgentMail port 8765"),
    ),
)
def test_runtime_preflight_fails_before_artifact(
    tmp_path: Path,
    mode: str,
    port: int,
    message: str,
) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    env_file = _environment(tmp_path / "mail.env", root, mode=mode, port=port)
    with pytest.raises(service.ServiceError, match=message):
        service.render_launchd(
            output_dir=tmp_path / "artifacts",
            service_executable=_executable(tmp_path / "service"),
            server_executable=_executable(tmp_path / "server"),
            env_file=env_file,
            state_root=root,
        )
    assert not (tmp_path / "artifacts").exists()


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
    ) -> None:
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        plist = plistlib.loads(Path(ownership["artifact"]).read_bytes())
        self.artifact = str(Path(ownership["artifact"]).resolve())
        self.arguments = list(plist["ProgramArguments"])
        self.running = running
        self.foreign = foreign
        self.print_returncode = print_returncode
        self.fail_operation = fail_operation
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        operation = arguments[1]
        if operation == "print":
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
                    "arguments = {",
                    *(f"\t{argument}" for argument in program_arguments),
                    "}",
                    f"note = expected tokens: {self.artifact} {' '.join(self.arguments)}",
                )
            )
            return subprocess.CompletedProcess(arguments, 0, record + "\n", "")
        if operation == self.fail_operation:
            return subprocess.CompletedProcess(arguments, 5, "", f"{operation} failed")
        if operation == "bootstrap":
            self.running = True
        elif operation == "bootout":
            self.running = False
        return subprocess.CompletedProcess(arguments, 0, "", "")


def test_owned_start_and_stop_use_explicit_launchctl_only(tmp_path: Path) -> None:
    rendered, _, _ = _rendered(tmp_path)
    ownership = Path(rendered.ownership_manifest)
    fake = _FakeLaunchctl(ownership, running=False)

    started = service.service_start(ownership, runner=fake)
    stopped = service.service_stop(ownership, runner=fake)

    assert started["status"] == "job_loaded"
    assert started["action"] == "started"
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
