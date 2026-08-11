"""Dormant launchd artifacts and an ownership-checked service controller.

Nothing in this module runs at import time.  Rendering does not invoke
``launchctl``; service-manager commands are explicit and are intended for the
later operator-approved cutover.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import plistlib
import pwd
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlparse


LAUNCHD_LABEL: Final[str] = "org.agentstack.mail"
OWNERSHIP_NAME: Final[str] = "org.agentstack.mail.ownership.json"
PLIST_NAME: Final[str] = "org.agentstack.mail.plist"
LEGACY_PORT: Final[int] = 8765
MIGRATION_STAGING_MARKER: Final[str] = ".agentstack-mail-migration-staging.json"


class ServiceError(RuntimeError):
    """A service artifact or ownership check failed."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    host: str
    port: int
    path: str
    database: Path
    archive: Path
    signals: Path
    state_root: Path


@dataclass(frozen=True, slots=True)
class RenderResult:
    status: str
    artifact: str
    ownership_manifest: str
    artifact_sha256: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    return first == second or first in second.parents or second in first.parents


def _user_homes() -> tuple[Path, ...]:
    homes = {Path.home().resolve(strict=False)}
    try:
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=False))
    except (KeyError, OSError):
        pass
    return tuple(sorted(homes, key=str))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_env_file(path: Path) -> dict[str, str]:
    path = path.expanduser().resolve(strict=False)
    if not path.is_file():
        raise ServiceError(f"environment file is missing: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ServiceError(f"malformed environment line {line_number}: {path}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key or any(character.isspace() for character in key):
            raise ServiceError(f"malformed environment key on line {line_number}: {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def _sqlite_path(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"sqlite", "sqlite+aiosqlite"}:
        raise ServiceError("service database must be a local SQLite URL")
    if parsed.netloc not in {"", "localhost"}:
        raise ServiceError("SQLite URL must not name a remote host")
    raw_path = unquote(parsed.path)
    if not raw_path.startswith("/"):
        raise ServiceError("SQLite URL must contain an absolute path")
    return Path(raw_path).resolve(strict=False)


def runtime_config(env_file: Path, state_root: Path) -> RuntimeConfig:
    """Validate the exact service namespace without exposing dotenv values."""

    values = _read_env_file(env_file)
    state_root = state_root.expanduser().resolve(strict=False)
    host = values.get("AGENTSTACK_MAIL_HTTP_HOST", "127.0.0.1").strip()
    try:
        port = int(values.get("AGENTSTACK_MAIL_HTTP_PORT", "18765"))
    except ValueError as exc:
        raise ServiceError("AGENTSTACK_MAIL_HTTP_PORT is not an integer") from exc
    path = values.get("AGENTSTACK_MAIL_HTTP_PATH", "/mcp").strip()
    mode = values.get("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE")
    if mode != "passthrough":
        raise ServiceError(
            "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough is required"
        )
    if not _loopback(host):
        raise ServiceError("service host must be loopback-only")
    if port == LEGACY_PORT:
        raise ServiceError("refusing the legacy AgentMail port 8765")
    if not 1 <= port <= 65535:
        raise ServiceError("service port is outside 1..65535")
    if not path.startswith("/") or not path:
        raise ServiceError("service HTTP path must start with '/'")

    expected_database = (state_root / "storage.sqlite3").resolve(strict=False)
    expected_archive = (state_root / "archive").resolve(strict=False)
    expected_signals = (state_root / "signals").resolve(strict=False)
    database = _sqlite_path(
        values.get(
            "AGENTSTACK_MAIL_DATABASE_URL",
            f"sqlite+aiosqlite:///{expected_database}",
        )
    )
    archive = Path(
        values.get("AGENTSTACK_MAIL_STORAGE_ROOT", str(expected_archive))
    ).expanduser().resolve(strict=False)
    signals = Path(
        values.get(
            "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(expected_signals)
        )
    ).expanduser().resolve(strict=False)
    if (database, archive, signals) != (
        expected_database,
        expected_archive,
        expected_signals,
    ):
        raise ServiceError(
            "database, archive, and signals must be the canonical children of state-root"
        )

    legacy_roots = tuple(
        path.resolve(strict=False)
        for home in _user_homes()
        for path in (
            home / "mcp_agent_mail",
            home / ".mcp_agent_mail_git_mailbox_repo",
            home / ".mcp_agent_mail" / "signals",
        )
    )
    new_roots = (state_root, database, archive, signals)
    if any(_overlap(new, legacy) for new in new_roots for legacy in legacy_roots):
        raise ServiceError("refusing overlap with a legacy AgentMail writable root")
    return RuntimeConfig(
        host=host,
        port=port,
        path=path,
        database=database,
        archive=archive,
        signals=signals,
        state_root=state_root,
    )


def _require_absolute_executable(path: Path, description: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ServiceError(f"{description} must be an absolute path")
    path = path.resolve(strict=False)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ServiceError(f"{description} is not an executable file: {path}")
    return path


def _atomic_content_write(path: Path, payload: bytes, mode: int) -> str:
    digest = _sha256_bytes(payload)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ServiceError(f"refusing non-regular artifact path: {path}")
        if path.read_bytes() == payload:
            if stat_mode(path) == mode:
                return "noop"
            os.chmod(path, mode)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            parent_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            return "mode-repaired"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "written"


def render_launchd(
    *,
    output_dir: Path,
    service_executable: Path,
    server_executable: Path,
    env_file: Path,
    state_root: Path,
) -> RenderResult:
    """Render a launchd definition and ownership record without registering it."""

    service_executable = _require_absolute_executable(
        service_executable, "service executable"
    )
    server_executable = _require_absolute_executable(server_executable, "server executable")
    env_file = env_file.expanduser().resolve(strict=False)
    config = runtime_config(env_file, state_root)
    output_dir = output_dir.expanduser().resolve(strict=False)
    live_launchagents = tuple(
        (home / "Library" / "LaunchAgents").resolve(strict=False)
        for home in _user_homes()
    )
    if any(
        output_dir == live or live in output_dir.parents for live in live_launchagents
    ):
        raise ServiceError("render output must be a staging directory, not live LaunchAgents")
    directory_changed = False
    if output_dir.exists():
        if not output_dir.is_dir() or output_dir.is_symlink():
            raise ServiceError(f"render output is not a real directory: {output_dir}")
        if stat_mode(output_dir) != 0o700:
            os.chmod(output_dir, 0o700)
            directory_changed = True
    else:
        output_dir.mkdir(mode=0o700, parents=True)
        os.chmod(output_dir, 0o700)
        directory_changed = True
    runtime_dir = config.state_root / "runtime"
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(service_executable),
            "foreground",
            "--server-executable",
            str(server_executable),
            "--env-file",
            str(env_file),
            "--state-root",
            str(config.state_root),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(runtime_dir / "service.stdout.log"),
        "StandardErrorPath": str(runtime_dir / "service.stderr.log"),
        "EnvironmentVariables": {
            "AGENTSTACK_MAIL_ENV_FILE": str(env_file),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LC_ALL": "C",
        },
    }
    artifact_payload = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    artifact_path = output_dir / PLIST_NAME
    artifact_status = _atomic_content_write(artifact_path, artifact_payload, 0o644)
    artifact_digest = _sha256_bytes(artifact_payload)
    ownership = {
        "schema_version": 1,
        "tool": "agentstack-mail-service",
        "platform": "launchd",
        "label": LAUNCHD_LABEL,
        "artifact": str(artifact_path),
        "artifact_sha256": artifact_digest,
        "service_executable": str(service_executable),
        "server_executable": str(server_executable),
        "env_file": str(env_file),
        "env_sha256": _sha256_bytes(env_file.read_bytes()),
        "state_root": str(config.state_root),
        "endpoint": f"http://{config.host}:{config.port}{config.path}",
    }
    ownership_path = output_dir / OWNERSHIP_NAME
    ownership_status = _atomic_content_write(
        ownership_path,
        json.dumps(ownership, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        0o600,
    )
    return RenderResult(
        status=(
            "noop"
            if not directory_changed and artifact_status == ownership_status == "noop"
            else "rendered"
        ),
        artifact=str(artifact_path),
        ownership_manifest=str(ownership_path),
        artifact_sha256=artifact_digest,
    )


def _load_ownership(path: Path) -> dict[str, Any]:
    try:
        ownership = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"cannot read ownership manifest {path}: {exc}") from exc
    required = {
        "schema_version": 1,
        "tool": "agentstack-mail-service",
        "platform": "launchd",
        "label": LAUNCHD_LABEL,
    }
    if not isinstance(ownership, dict) or any(
        ownership.get(key) != value for key, value in required.items()
    ):
        raise ServiceError("ownership manifest has the wrong identity")
    artifact = Path(str(ownership.get("artifact", ""))).resolve(strict=False)
    if not artifact.is_file():
        raise ServiceError(f"owned service artifact is missing: {artifact}")
    digest = _sha256_bytes(artifact.read_bytes())
    if digest != ownership.get("artifact_sha256"):
        raise ServiceError("owned service artifact digest has changed")
    try:
        plist = plistlib.loads(artifact.read_bytes())
    except Exception as exc:
        raise ServiceError(f"owned service artifact is not a valid plist: {exc}") from exc
    expected_arguments = plist.get("ProgramArguments") if isinstance(plist, dict) else None
    if (
        plist.get("Label") != LAUNCHD_LABEL
        or not isinstance(expected_arguments, list)
        or not all(isinstance(argument, str) for argument in expected_arguments)
    ):
        raise ServiceError("owned service artifact has an invalid launchd identity")
    ownership["_artifact_path"] = str(artifact)
    ownership["_expected_arguments"] = expected_arguments
    return ownership


def _environment_drift(ownership: Mapping[str, Any]) -> bool:
    env_file = Path(str(ownership.get("env_file", ""))).resolve(strict=False)
    try:
        return (
            not env_file.is_file()
            or _sha256_bytes(env_file.read_bytes()) != ownership.get("env_sha256")
        )
    except OSError:
        return True


def _unquote_launchctl_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_launchd_record(output: str) -> tuple[str | None, str | None, list[str] | None]:
    path: str | None = None
    program: str | None = None
    arguments: list[str] | None = None
    lines = output.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("path = "):
            path = _unquote_launchctl_value(stripped[7:])
        elif stripped.startswith("program = "):
            program = _unquote_launchctl_value(stripped[10:])
        elif stripped == "arguments = {":
            parsed: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != "}":
                value = _unquote_launchctl_value(lines[index].strip())
                if value:
                    parsed.append(value)
                index += 1
            arguments = parsed
        index += 1
    return path, program, arguments


def _launchctl_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 113


def _launchctl(
    arguments: Sequence[str],
    *,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is None:
        runner = subprocess.run
    return runner(
        ["launchctl", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def service_status(
    ownership_path: Path, *, runner: Runner | None = None
) -> dict[str, Any]:
    ownership = _load_ownership(ownership_path)
    env_drift = _environment_drift(ownership)
    identity = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    result = _launchctl(["print", identity], runner=runner)
    if result.returncode != 0:
        if _launchctl_not_found(result):
            return {
                "status": "stopped",
                "owned": True,
                "label": LAUNCHD_LABEL,
                "environment_drift": env_drift,
            }
        raise ServiceError(
            f"launchctl print failed; job state is unknown: {result.stderr.strip()}"
        )
    loaded_path, program, arguments = _parse_launchd_record(result.stdout)
    expected_arguments = list(ownership["_expected_arguments"])
    if (
        loaded_path != ownership["_artifact_path"]
        or program != expected_arguments[0]
        or arguments != expected_arguments
    ):
        return {
            "status": "foreign_or_unknown",
            "owned": False,
            "label": LAUNCHD_LABEL,
            "environment_drift": env_drift,
        }
    return {
        "status": "job_loaded",
        "owned": True,
        "label": LAUNCHD_LABEL,
        "environment_drift": env_drift,
        "mcp_readiness": "unverified",
    }


def _compensate_bootstrap(
    ownership_path: Path,
    *,
    runner: Runner,
) -> str:
    current = service_status(ownership_path, runner=runner)
    if current["status"] != "job_loaded" or not current["owned"]:
        return "loaded job could not be proven owned; no bootout attempted"
    identity = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    bootout = _launchctl(["bootout", identity], runner=runner)
    if bootout.returncode != 0:
        return f"owned compensation bootout failed: {bootout.stderr.strip()}"
    stopped = service_status(ownership_path, runner=runner)
    if stopped["status"] != "stopped":
        return "owned compensation bootout did not reach stopped state"
    return "owned bootstrap was compensated and verified stopped"


def service_start(
    ownership_path: Path, *, runner: Runner | None = None
) -> dict[str, Any]:
    if runner is None:
        runner = subprocess.run
    ownership = _load_ownership(ownership_path)
    if _environment_drift(ownership):
        raise ServiceError(
            "environment file changed after render; re-render before starting"
        )
    runtime_config(
        Path(str(ownership["env_file"])),
        Path(str(ownership["state_root"])),
    )
    current = service_status(ownership_path, runner=runner)
    if current["status"] == "job_loaded":
        return {**current, "action": "noop"}
    if current["status"] != "stopped":
        raise ServiceError("refusing to replace a foreign or unknown launchd job")
    domain = f"gui/{os.getuid()}"
    artifact = str(ownership["artifact"])
    commands = (
        ["bootstrap", domain, artifact],
        ["enable", f"{domain}/{LAUNCHD_LABEL}"],
        ["kickstart", f"{domain}/{LAUNCHD_LABEL}"],
    )
    bootstrapped = False
    for arguments in commands:
        result = _launchctl(arguments, runner=runner)
        if result.returncode != 0:
            compensation = (
                _compensate_bootstrap(ownership_path, runner=runner)
                if bootstrapped
                else "bootstrap did not succeed; no compensation needed"
            )
            raise ServiceError(
                f"launchctl {' '.join(arguments)} failed: {result.stderr.strip()}; "
                f"{compensation}"
            )
        if arguments[0] == "bootstrap":
            bootstrapped = True
    started = service_status(ownership_path, runner=runner)
    if started["status"] != "job_loaded" or not started["owned"]:
        raise ServiceError("launchd job did not reach the exact owned job state")
    return {**started, "action": "started"}


def service_stop(
    ownership_path: Path, *, runner: Runner | None = None
) -> dict[str, Any]:
    if runner is None:
        runner = subprocess.run
    _load_ownership(ownership_path)
    current = service_status(ownership_path, runner=runner)
    if current["status"] == "stopped":
        return {**current, "action": "noop"}
    if current["status"] != "job_loaded" or not current["owned"]:
        raise ServiceError("refusing to stop a foreign or unknown launchd job")
    identity = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    result = _launchctl(["bootout", identity], runner=runner)
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootout failed: {result.stderr.strip()}")
    stopped = service_status(ownership_path, runner=runner)
    if stopped["status"] != "stopped":
        raise ServiceError("launchd job did not reach the stopped state")
    return {
        "status": "stopped",
        "owned": True,
        "label": LAUNCHD_LABEL,
        "action": "stopped",
    }


def stat_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process_group: int, signum: int) -> None:
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        pass


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = 10.0,
    term_already_sent: bool = False,
) -> None:
    """Terminate the whole child session and reap the direct child."""

    process_group = process.pid
    if not _process_group_exists(process_group):
        if process.poll() is None:
            process.wait(timeout=grace_seconds)
        return
    if not term_already_sent:
        _signal_process_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    if process.poll() is None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            _signal_process_group(process_group, signal.SIGKILL)
            process.wait()
            return
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(process_group):
        _signal_process_group(process_group, signal.SIGKILL)


def foreground(
    *,
    server_executable: Path,
    env_file: Path,
    state_root: Path,
) -> int:
    """Hold the service writer lock, supervise the server, and forward signals."""

    server_executable = _require_absolute_executable(server_executable, "server executable")
    config = runtime_config(env_file, state_root)
    if not config.database.is_file() or not config.archive.is_dir():
        raise ServiceError("verified migrated database and archive must exist before service start")
    if (config.state_root / MIGRATION_STAGING_MARKER).exists():
        raise ServiceError(
            "migration publication is unconfirmed; rerun migration copy before service start"
        )
    runtime_dir = config.state_root / "runtime"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runtime_dir, 0o700)
    lock_path = runtime_dir / "authority.lock"
    lock_handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServiceError("another service process owns this state root") from exc
        environment = os.environ.copy()
        environment["AGENTSTACK_MAIL_ENV_FILE"] = str(env_file.resolve(strict=False))
        process = subprocess.Popen(
            [str(server_executable)],
            env=environment,
            start_new_session=True,
        )
        shutdown_signal: int | None = None

        def forward(signum: int, _frame: Any) -> None:
            nonlocal shutdown_signal
            shutdown_signal = signum
            _signal_process_group(process.pid, signum)

        previous = {
            signum: signal.signal(signum, forward)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            while True:
                try:
                    return process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    if shutdown_signal is not None:
                        _terminate_process_group(process, term_already_sent=True)
                        return int(
                            process.returncode
                            if process.returncode is not None
                            else -shutdown_signal
                        )
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            _terminate_process_group(process)
    finally:
        lock_handle.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentstack-mail-service",
        description="Render or explicitly control the AgentStack Mail launchd service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--output-dir", required=True)
    render.add_argument("--service-executable", required=True)
    render.add_argument("--server-executable", required=True)
    render.add_argument("--env-file", required=True)
    render.add_argument("--state-root", required=True)
    foreground_parser = subparsers.add_parser("foreground")
    foreground_parser.add_argument("--server-executable", required=True)
    foreground_parser.add_argument("--env-file", required=True)
    foreground_parser.add_argument("--state-root", required=True)
    for name in ("start", "stop", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--ownership-manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            result: Any = asdict(
                render_launchd(
                    output_dir=Path(args.output_dir),
                    service_executable=Path(args.service_executable),
                    server_executable=Path(args.server_executable),
                    env_file=Path(args.env_file),
                    state_root=Path(args.state_root),
                )
            )
        elif args.command == "foreground":
            raise SystemExit(
                foreground(
                    server_executable=Path(args.server_executable),
                    env_file=Path(args.env_file),
                    state_root=Path(args.state_root),
                )
            )
        elif args.command == "start":
            result = service_start(Path(args.ownership_manifest))
        elif args.command == "stop":
            result = service_stop(Path(args.ownership_manifest))
        else:
            result = service_status(Path(args.ownership_manifest))
    except (OSError, ServiceError, subprocess.SubprocessError) as exc:
        print(f"agentstack-mail-service: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
