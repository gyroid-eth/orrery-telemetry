"""Dormant launchd artifacts and an ownership-checked service controller.

Nothing in this module runs at import time.  Rendering does not invoke
``launchctl``; service-manager commands are explicit and are intended for the
later operator-approved cutover.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import plistlib
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import unquote, urlparse


LAUNCHD_LABEL: Final[str] = "org.orrery.mail"
LAUNCHD_REHEARSAL_PREFIX: Final[str] = f"{LAUNCHD_LABEL}.rehearsal."
OWNERSHIP_NAME: Final[str] = f"{LAUNCHD_LABEL}.ownership.json"
# File-descriptor ceiling written into the launchd job. See the plist body for
# why the inherited default is not survivable.
SOFT_MAX_OPEN_FILES: Final[int] = 8192
HARD_MAX_OPEN_FILES: Final[int] = 16384
PLIST_NAME: Final[str] = f"{LAUNCHD_LABEL}.plist"
LEGACY_PORT: Final[int] = 8765
MIGRATION_STAGING_MARKER: Final[str] = ".agentstack-mail-migration-staging.json"


class ServiceError(RuntimeError):
    """A service artifact or ownership check failed."""


class HandoffConflictError(ServiceError):
    """A same-port observation found another authority; retrying is unsafe."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    host: str
    port: int
    path: str
    database: Path
    archive: Path
    signals: Path
    state_root: Path
    legacy_launchd_label: str | None
    legacy_launchd_receipt: Path | None
    legacy_launchd_receipt_sha256: str | None


@dataclass(frozen=True, slots=True)
class RenderResult:
    status: str
    artifact: str
    ownership_manifest: str
    artifact_sha256: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _launchd_label(label: str) -> str:
    if label == LAUNCHD_LABEL:
        return label
    if not label.startswith(LAUNCHD_REHEARSAL_PREFIX):
        raise ServiceError(
            f"custom launchd label must use {LAUNCHD_REHEARSAL_PREFIX!r}"
        )
    suffix = label.removeprefix(LAUNCHD_REHEARSAL_PREFIX)
    segment = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    if (
        len(label) > 128
        or re.fullmatch(rf"{segment}(?:\.{segment})*", suffix) is None
    ):
        raise ServiceError("custom launchd label has an invalid rehearsal suffix")
    return label


def rehearsal_launchd_label(label: str) -> str:
    """Validate a non-production label reserved for an isolated rehearsal."""

    label = _launchd_label(label)
    if label == LAUNCHD_LABEL:
        raise ServiceError("rehearsal label must not equal the production launchd label")
    return label


def _external_launchd_label(label: str) -> str:
    """Validate a configured launchd identity without assuming a user-specific name."""

    segment = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    if len(label) > 128 or re.fullmatch(rf"{segment}(?:\.{segment})*", label) is None:
        raise ServiceError("configured legacy launchd label is invalid")
    if label == LAUNCHD_LABEL:
        raise ServiceError("legacy launchd label must differ from the new service label")
    return label


def _launchd_artifact_names(label: str) -> tuple[str, str]:
    if label == LAUNCHD_LABEL:
        return PLIST_NAME, OWNERSHIP_NAME
    return f"{label}.plist", f"{label}.ownership.json"


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
    legacy_launchd_label = (
        values.get("AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL", "").strip() or None
    )
    if legacy_launchd_label is not None:
        legacy_launchd_label = _external_launchd_label(legacy_launchd_label)
    legacy_receipt_raw = values.get(
        "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT", ""
    ).strip()
    legacy_launchd_receipt = Path(legacy_receipt_raw).expanduser() if legacy_receipt_raw else None
    legacy_launchd_receipt_sha256 = (
        values.get("AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT_SHA256", "").strip()
        or None
    )
    if (legacy_launchd_receipt is None) != (legacy_launchd_receipt_sha256 is None):
        raise ServiceError(
            "legacy launchd receipt path and SHA-256 must be configured together"
        )
    if legacy_launchd_receipt is not None and not legacy_launchd_receipt.is_absolute():
        raise ServiceError("legacy launchd receipt must be an absolute path")
    if legacy_launchd_receipt_sha256 is not None and (
        len(legacy_launchd_receipt_sha256) != 64
        or set(legacy_launchd_receipt_sha256) - set("0123456789abcdef")
    ):
        raise ServiceError("legacy launchd receipt SHA-256 must be lowercase hex")
    mode = values.get("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE")
    if mode != "passthrough":
        raise ServiceError(
            "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough is required"
        )
    if not _loopback(host):
        raise ServiceError("service host must be loopback-only")
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
        legacy_launchd_label=legacy_launchd_label,
        legacy_launchd_receipt=legacy_launchd_receipt,
        legacy_launchd_receipt_sha256=legacy_launchd_receipt_sha256,
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
    label: str = LAUNCHD_LABEL,
) -> RenderResult:
    """Render a launchd definition and ownership record without registering it."""

    label = _launchd_label(label)
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
        "Label": label,
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
        # A launchd job inherits launchd's own file-descriptor default, which is
        # 256 on macOS — not the shell's soft limit, which is orders of
        # magnitude higher and so hides the ceiling during development. The
        # server opens a git repository to issue a file reservation, so once it
        # crosses that ceiling reservations fail with EMFILE while health_check
        # keeps answering off descriptors it already holds: a server that looks
        # healthy and cannot hand out reservations, which fail-closes every
        # agent edit behind the PreToolUse hook. Reported from an install that
        # reached 272 open regular files after 7.75 days (2026-08-24). Raising
        # the ceiling does not excuse a leak; it turns a multi-day outage into
        # something an operator has time to notice.
        "SoftResourceLimits": {"NumberOfFiles": SOFT_MAX_OPEN_FILES},
        "HardResourceLimits": {"NumberOfFiles": HARD_MAX_OPEN_FILES},
        "StandardOutPath": str(runtime_dir / "service.stdout.log"),
        "StandardErrorPath": str(runtime_dir / "service.stderr.log"),
        "EnvironmentVariables": {
            "AGENTSTACK_MAIL_ENV_FILE": str(env_file),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LC_ALL": "C",
        },
    }
    artifact_payload = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    plist_name, ownership_name = _launchd_artifact_names(label)
    artifact_path = output_dir / plist_name
    artifact_status = _atomic_content_write(artifact_path, artifact_payload, 0o644)
    artifact_digest = _sha256_bytes(artifact_payload)
    ownership = {
        "schema_version": 1,
        "tool": "agentstack-mail-service",
        "platform": "launchd",
        "label": label,
        "artifact": str(artifact_path),
        "artifact_sha256": artifact_digest,
        "service_executable": str(service_executable),
        "server_executable": str(server_executable),
        "env_file": str(env_file),
        "env_sha256": _sha256_bytes(env_file.read_bytes()),
        "state_root": str(config.state_root),
        "endpoint": f"http://{config.host}:{config.port}{config.path}",
    }
    ownership_path = output_dir / ownership_name
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


def _load_ownership(
    path: Path,
    *,
    expected_label: str = LAUNCHD_LABEL,
) -> dict[str, Any]:
    expected_label = _launchd_label(expected_label)
    try:
        ownership = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"cannot read ownership manifest {path}: {exc}") from exc
    required = {
        "schema_version": 1,
        "tool": "agentstack-mail-service",
        "platform": "launchd",
        "label": expected_label,
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
        plist.get("Label") != expected_label
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


def _parse_launchd_pid(output: str) -> int | None:
    pids: list[int] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                pid = int(stripped[6:].strip())
            except ValueError:
                raise ServiceError("launchd returned a malformed wrapper PID") from None
            if pid <= 0:
                raise ServiceError("launchd returned a non-positive wrapper PID")
            pids.append(pid)
    if len(pids) > 1:
        raise ServiceError("launchd returned multiple wrapper PIDs")
    return pids[0] if pids else None


def _listener_process_ids(
    port: int,
    *,
    timeout_seconds: float = 5.0,
) -> list[int]:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run(
            [executable, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(
            f"cannot inspect listeners on port {port}: {exc}"
        ) from exc
    if result.returncode == 1 and not result.stdout.strip():
        return []
    if result.returncode != 0:
        raise ServiceError(
            f"cannot inspect listeners on port {port}: {result.stderr.strip()}"
        )
    try:
        return sorted({int(line) for line in result.stdout.splitlines() if line.strip()})
    except ValueError as exc:
        raise ServiceError(f"listener inspection returned an invalid PID for port {port}") from exc


def _process_parent_pid(
    process_id: int,
    *,
    timeout_seconds: float = 5.0,
) -> int:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run(
            [executable, "-nP", "-p", str(process_id), "-FpR"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(
            f"cannot inspect parent of listener PID {process_id}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise ServiceError(
            f"cannot inspect parent of listener PID {process_id}: {result.stderr.strip()}"
        )
    try:
        parent = next(
            int(line[1:]) for line in result.stdout.splitlines() if line.startswith("R")
        )
    except (StopIteration, ValueError) as exc:
        raise ServiceError(
            f"listener PID {process_id} returned an invalid parent process"
        ) from exc
    if parent <= 0:
        raise ServiceError(f"listener PID {process_id} has no valid parent process")
    return parent


def _mcp_health_probe(
    config: RuntimeConfig,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Read the exact distributed MCP path without changing coordination state."""

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "service-start-readiness",
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
    ).encode("utf-8")
    endpoint = f"http://{config.host}:{config.port}{config.path}"
    request = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib_error.URLError) as exc:
        raise ServiceError(f"MCP readiness request failed at {endpoint}: {exc}") from exc
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError("MCP readiness returned malformed JSON") from exc
    if not isinstance(envelope, dict) or "error" in envelope:
        raise ServiceError("MCP readiness returned a JSON-RPC error")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise ServiceError("MCP readiness returned a failed tool result")
    health = result.get("structuredContent")
    if health is None:
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    health = json.loads(block.get("text") or "")
                except json.JSONDecodeError as exc:
                    raise ServiceError(
                        "MCP readiness returned malformed text content"
                    ) from exc
                break
    expected_database = f"sqlite+aiosqlite:///{config.database}"
    if health != {
        "status": "ok",
        "environment": health.get("environment") if isinstance(health, dict) else None,
        "http_host": config.host,
        "http_port": config.port,
        "database_url": expected_database,
    }:
        raise ServiceError("MCP readiness did not match the expected path and database")
    return {
        "status": "verified",
        "endpoint": endpoint,
        "http_host": config.host,
        "http_port": config.port,
        "database_url": expected_database,
    }


def _launchctl_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 113


def _launchctl(
    arguments: Sequence[str],
    *,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is None:
        runner = subprocess.run
    try:
        return runner(
            ["launchctl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        operation = arguments[0] if arguments else "request"
        raise ServiceError(f"launchctl {operation} could not complete: {exc}") from exc


def require_rehearsal_job_absent(
    label: str,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Fail closed unless the exact non-production launchd identity is absent."""

    label = rehearsal_launchd_label(label)
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["print", identity], runner=runner)
    if _launchctl_not_found(result):
        return {"status": "absent", "label": label, "identity": identity}
    if result.returncode == 0:
        raise ServiceError(f"rehearsal launchd job already exists: {identity}")
    raise ServiceError(
        f"rehearsal launchd job state is unknown: {result.stderr.strip()}"
    )


def service_status(
    ownership_path: Path,
    *,
    label: str = LAUNCHD_LABEL,
    runner: Runner | None = None,
) -> dict[str, Any]:
    label = _launchd_label(label)
    ownership = _load_ownership(ownership_path, expected_label=label)
    env_drift = _environment_drift(ownership)
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["print", identity], runner=runner)
    if result.returncode != 0:
        if _launchctl_not_found(result):
            return {
                "status": "stopped",
                "owned": True,
                "label": label,
                "environment_drift": env_drift,
                "launchctl_print_returncode": result.returncode,
                "launchctl_print_state": "absent",
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
            "label": label,
            "environment_drift": env_drift,
            "launchctl_print_returncode": result.returncode,
            "launchctl_print_state": "loaded",
        }
    return {
        "status": "job_loaded",
        "owned": True,
        "label": label,
        "launchd_pid": _parse_launchd_pid(result.stdout),
        "environment_drift": env_drift,
        "mcp_readiness": "unverified",
        "launchctl_print_returncode": result.returncode,
        "launchctl_print_state": "loaded",
    }


def _compensate_bootstrap(
    ownership_path: Path,
    *,
    label: str,
    runner: Runner,
) -> str:
    try:
        current = service_status(ownership_path, label=label, runner=runner)
    except ServiceError as exc:
        return f"loaded job could not be inspected for compensation: {exc}"
    if current["status"] != "job_loaded" or not current["owned"]:
        return "loaded job could not be proven owned; no bootout attempted"
    identity = f"gui/{os.getuid()}/{label}"
    try:
        bootout = _launchctl(["bootout", identity], runner=runner)
    except ServiceError as exc:
        return f"owned compensation bootout could not complete: {exc}"
    if bootout.returncode != 0:
        return f"owned compensation bootout failed: {bootout.stderr.strip()}"
    try:
        _wait_for_service_stopped(
            ownership_path,
            label=label,
            runner=runner,
        )
    except ServiceError as exc:
        return f"owned compensation bootout did not reach stopped state: {exc}"
    return "owned bootstrap was compensated and verified stopped"


def _safe_compensate_bootstrap(
    ownership_path: Path,
    *,
    label: str,
    runner: Runner,
) -> str:
    """Attempt compensation without allowing a second exception to mask the first."""

    try:
        return _compensate_bootstrap(
            ownership_path,
            label=label,
            runner=runner,
        )
    except BaseException as exc:
        return (
            "compensation itself did not complete: "
            f"{type(exc).__name__}: {exc}"
        )


def _wait_for_service_stopped(
    ownership_path: Path,
    *,
    label: str,
    runner: Runner,
    timeout_seconds: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for launchd's asynchronous bootout without accepting ownership drift."""

    if timeout_seconds <= 0:
        raise ServiceError("launchd stop timeout must be positive")
    started = time.monotonic()
    polls = 0
    while True:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise ServiceError("launchd job did not retire before the stop deadline")

        def bounded_runner(
            arguments: Sequence[str],
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            kwargs["timeout"] = min(float(kwargs.get("timeout", remaining)), remaining)
            return runner(arguments, **kwargs)

        polls += 1
        current = service_status(ownership_path, label=label, runner=bounded_runner)
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise ServiceError("launchd job did not retire before the stop deadline")
        if current["status"] == "stopped":
            return current, {
                "poll_count": polls,
                "bounded_stopped_ms": round(elapsed * 1000, 3),
                "deadline_seconds": timeout_seconds,
            }
        if current["status"] != "job_loaded" or not current["owned"]:
            raise ServiceError("launchd label changed ownership while waiting for bootout")
        if time.monotonic() - started >= timeout_seconds:
            raise ServiceError("launchd job did not retire before the stop deadline")
        time.sleep(0.05)


def _verified_legacy_launchd_receipt(config: RuntimeConfig) -> dict[str, Any]:
    """Bind the configured legacy label to one sealed C1 definition receipt."""

    label = config.legacy_launchd_label
    receipt_path = config.legacy_launchd_receipt
    expected_sha256 = config.legacy_launchd_receipt_sha256
    if label is None or receipt_path is None or expected_sha256 is None:
        raise ServiceError(
            "same-port production start requires the legacy launchd label, sealed "
            "receipt path, and receipt SHA-256"
        )
    try:
        resolved = receipt_path.resolve(strict=True)
        info = receipt_path.lstat()
    except OSError as exc:
        raise ServiceError(f"legacy launchd receipt is unavailable: {receipt_path}") from exc
    if resolved != receipt_path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ServiceError(
            "legacy launchd receipt must be one canonical, non-linked regular file"
        )
    if stat.S_IMODE(info.st_mode) != 0o400:
        raise ServiceError("legacy launchd receipt must retain write-once mode 0400")
    try:
        payload = receipt_path.read_bytes()
    except OSError as exc:
        raise ServiceError(f"legacy launchd receipt is unreadable: {receipt_path}") from exc
    actual_sha256 = _sha256_bytes(payload)
    if actual_sha256 != expected_sha256:
        raise ServiceError("legacy launchd receipt SHA-256 does not match configuration")
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError("legacy launchd receipt is malformed JSON") from exc
    definition = receipt.get("definition") if isinstance(receipt, dict) else None
    runtime = receipt.get("runtime") if isinstance(receipt, dict) else None
    if (
        not isinstance(definition, dict)
        or not isinstance(runtime, dict)
        or receipt.get("kind") != "legacy-launchd-definition"
        or receipt.get("cutover_eligible") is not True
        or definition.get("state") != "loaded"
        or definition.get("loaded_path_program_arguments_match_plist") is not True
        or runtime.get("listener_port") != LEGACY_PORT
        or runtime.get("listener_is_wrapper_child") is not True
        or runtime.get("network_requests_sent") != 0
    ):
        raise ServiceError("legacy launchd receipt is not cutover-eligible")
    sealed_label = definition.get("label")
    if sealed_label != label:
        raise ServiceError(
            "configured legacy launchd label does not match the sealed C1 receipt: "
            f"configured={label!r}, sealed={sealed_label!r}"
        )
    return {
        "status": "verified",
        "path": str(receipt_path),
        "sha256": actual_sha256,
        "definition_label": sealed_label,
    }


def _require_legacy_job_absent(
    legacy_label: str,
    *,
    runner: Runner,
) -> dict[str, Any]:
    identity = f"gui/{os.getuid()}/{legacy_label}"
    result = _launchctl(["print", identity], runner=runner)
    if _launchctl_not_found(result):
        return {
            "label": legacy_label,
            "identity": identity,
            "launchctl_print_returncode": result.returncode,
            "state": "absent",
        }
    if result.returncode == 0:
        raise HandoffConflictError(
            f"same-port handoff blocked: bootout legacy launchd job {legacy_label!r} "
            "and verify port 8765 is free before starting"
        )
    raise HandoffConflictError(
        "same-port handoff blocked: legacy launchd job state is unknown; "
        f"bootout {legacy_label!r} only after identifying its exact state"
    )


def _same_port_runtime_snapshot(
    ownership_path: Path,
    config: RuntimeConfig,
    *,
    label: str,
    runner: Runner,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Require one stable, ready authority across two topology observations."""

    legacy_label = config.legacy_launchd_label
    if legacy_label is None:
        raise ServiceError("same-port runtime snapshot lacks a legacy launchd label")

    def remaining(default: float) -> float:
        if deadline is None:
            return default
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise ServiceError("same-port readiness deadline expired")
        return min(default, budget)

    def bounded_runner(
        arguments: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        requested = float(kwargs.get("timeout", 20.0))
        kwargs["timeout"] = remaining(requested)
        return runner(arguments, **kwargs)

    first_job = service_status(ownership_path, label=label, runner=bounded_runner)
    if (
        first_job["status"] != "job_loaded"
        or not first_job["owned"]
        or first_job.get("environment_drift") is not False
    ):
        raise ServiceError("same-port new launchd job is not exact and drift-free")
    wrapper_pid = first_job.get("launchd_pid")
    if isinstance(wrapper_pid, bool) or not isinstance(wrapper_pid, int):
        raise ServiceError("same-port new launchd wrapper PID is unavailable")

    first_legacy = _require_legacy_job_absent(
        legacy_label,
        runner=bounded_runner,
    )
    first_listeners = _listener_process_ids(
        LEGACY_PORT,
        timeout_seconds=remaining(5.0),
    )
    if not first_listeners:
        raise ServiceError(
            "same-port readiness requires exactly one listener on port 8765"
        )
    if len(first_listeners) != 1:
        raise HandoffConflictError(
            "same-port readiness found multiple listeners on port 8765"
        )
    listener_pid = first_listeners[0]
    if _process_parent_pid(
        listener_pid,
        timeout_seconds=remaining(5.0),
    ) != wrapper_pid:
        raise HandoffConflictError(
            f"same-port listener PID {listener_pid} is not a child of the owned "
            "launchd wrapper"
        )

    health = _mcp_health_probe(config, timeout_seconds=remaining(2.0))

    second_job = service_status(ownership_path, label=label, runner=bounded_runner)
    if (
        second_job["status"] != "job_loaded"
        or not second_job["owned"]
        or second_job.get("environment_drift") is not False
        or second_job.get("launchd_pid") != wrapper_pid
    ):
        raise ServiceError("same-port launchd identity changed during readiness")
    second_legacy = _require_legacy_job_absent(
        legacy_label,
        runner=bounded_runner,
    )
    second_listeners = _listener_process_ids(
        LEGACY_PORT,
        timeout_seconds=remaining(5.0),
    )
    if second_listeners != [listener_pid]:
        raise ServiceError("same-port listener identity changed during readiness")
    if _process_parent_pid(
        listener_pid,
        timeout_seconds=remaining(5.0),
    ) != wrapper_pid:
        raise ServiceError(
            "same-port listener parent changed during readiness"
        )
    remaining(0.0)
    return {
        "status": "verified",
        "wrapper_pid": wrapper_pid,
        "listener_pid": listener_pid,
        "listener_observations": [first_listeners, second_listeners],
        "legacy_observations": [first_legacy, second_legacy],
        "mcp": health,
    }


def _wait_for_same_port_ready(
    ownership_path: Path,
    config: RuntimeConfig,
    *,
    label: str,
    runner: Runner,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ServiceError("same-port readiness timeout must be positive")
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_error = "endpoint did not answer"
    while time.monotonic() < deadline:
        try:
            snapshot = _same_port_runtime_snapshot(
                ownership_path,
                config,
                label=label,
                runner=runner,
                deadline=deadline,
            )
        except HandoffConflictError:
            raise
        except ServiceError as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
            continue
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise ServiceError("same-port readiness deadline expired after verification")
        return {
            **snapshot,
            "bounded_ready_ms": round(elapsed * 1000, 3),
            "deadline_seconds": timeout_seconds,
        }
    raise ServiceError(f"same-port readiness deadline expired: {last_error}")


def _same_port_start_preflight(
    config: RuntimeConfig,
    current: Mapping[str, Any],
    *,
    legacy_receipt: Mapping[str, Any],
    runner: Runner,
) -> dict[str, Any] | None:
    """Prevent the new launchd job from racing a legacy owner of port 8765."""

    if config.port != LEGACY_PORT:
        return None
    if config.path != "/api/":
        raise ServiceError(
            "same-port production start requires AGENTSTACK_MAIL_HTTP_PATH=/api/"
        )
    legacy_label = config.legacy_launchd_label
    if legacy_label is None:
        raise ServiceError(
            "same-port production start requires "
            "AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL; configure the legacy job "
            "identity, boot it out, and retry"
        )

    legacy = _require_legacy_job_absent(legacy_label, runner=runner)

    listeners = _listener_process_ids(LEGACY_PORT)
    if current["status"] == "stopped":
        foreign = listeners
    else:
        wrapper_pid = current.get("launchd_pid")
        if isinstance(wrapper_pid, bool) or not isinstance(wrapper_pid, int):
            raise ServiceError(
                "same-port handoff blocked: the owned launchd wrapper PID could not "
                "be proven"
            )
        if len(listeners) != 1:
            raise ServiceError(
                "same-port handoff blocked: a loaded owned job must have exactly one "
                "listener on port 8765"
            )
        foreign = [
            process_id
            for process_id in listeners
            if _process_parent_pid(process_id) != wrapper_pid
        ]
    if foreign:
        raise ServiceError(
            f"same-port handoff blocked: listener PIDs {foreign} on port 8765 are "
            "not children of the owned launchd job; bootout the legacy service and "
            "verify the port is free before starting"
        )
    return {
        "status": "accepted",
        "port": LEGACY_PORT,
        "path": config.path,
        "legacy_launchd_label": legacy_label,
        "legacy_launchd_state": legacy["state"],
        "legacy_launchd_receipt": dict(legacy_receipt),
        "listener_pids": listeners,
    }


def service_start(
    ownership_path: Path,
    *,
    label: str = LAUNCHD_LABEL,
    runner: Runner | None = None,
) -> dict[str, Any]:
    if runner is None:
        runner = subprocess.run
    label = _launchd_label(label)
    ownership = _load_ownership(ownership_path, expected_label=label)
    if _environment_drift(ownership):
        raise ServiceError(
            "environment file changed after render; re-render before starting"
        )
    config = runtime_config(
        Path(str(ownership["env_file"])),
        Path(str(ownership["state_root"])),
    )
    legacy_receipt: dict[str, Any] | None = None
    if config.port == LEGACY_PORT:
        if label != LAUNCHD_LABEL:
            raise ServiceError(
                "port 8765 is reserved for the production launchd identity; "
                "use an isolated port for rehearsal"
            )
        if config.host != "127.0.0.1":
            raise ServiceError(
                "same-port production start requires "
                "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1"
            )
        if config.path != "/api/":
            raise ServiceError(
                "same-port production start requires AGENTSTACK_MAIL_HTTP_PATH=/api/"
            )
        if config.legacy_launchd_label is None:
            raise ServiceError(
                "same-port production start requires "
                "AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL; configure the legacy job "
                "identity, boot it out, and retry"
            )
        legacy_receipt = _verified_legacy_launchd_receipt(config)
    current = service_status(ownership_path, label=label, runner=runner)
    if current["status"] not in {"job_loaded", "stopped"}:
        raise ServiceError("refusing to replace a foreign or unknown launchd job")
    same_port_preflight = _same_port_start_preflight(
        config,
        current,
        legacy_receipt=legacy_receipt or {},
        runner=runner,
    )
    if current["status"] == "job_loaded":
        same_port_readiness = (
            _wait_for_same_port_ready(
                ownership_path,
                config,
                label=label,
                runner=runner,
            )
            if same_port_preflight is not None
            else None
        )
        return {
            **current,
            "action": "noop",
            "mcp_readiness": same_port_readiness or current["mcp_readiness"],
            **(
                {"same_port_preflight": same_port_preflight}
                if same_port_preflight is not None
                else {}
            ),
        }
    domain = f"gui/{os.getuid()}"
    artifact = str(ownership["artifact"])
    commands = (
        ["bootstrap", domain, artifact],
        ["enable", f"{domain}/{label}"],
        ["kickstart", f"{domain}/{label}"],
    )
    bootstrap_outcome = "loaded"
    bootstrap_eio_recheck: dict[str, Any] | None = None
    failure_prefix = "launchd service mutation outcome is unknown"
    try:
        for arguments in commands:
            failure_prefix = f"launchctl {arguments[0]} outcome is unknown"
            result = _launchctl(arguments, runner=runner)
            if result.returncode != 0:
                if arguments[0] == "bootstrap" and result.returncode == errno.EIO:
                    after_eio = service_status(
                        ownership_path,
                        label=label,
                        runner=runner,
                    )
                    if after_eio["status"] == "job_loaded" and after_eio["owned"]:
                        if after_eio.get("environment_drift") is not False:
                            raise ServiceError(
                                "launchctl bootstrap returned EIO and the exact loaded "
                                "job has environment drift"
                            )
                        # EIO does not prove absence: an already-bootstrapped identity
                        # is one known cause. Only the exact loaded definition is safe
                        # to continue without issuing bootstrap a second time.
                        bootstrap_outcome = "exact_job_already_loaded_after_eio"
                        bootstrap_eio_recheck = after_eio
                        continue
                    if after_eio["status"] == "stopped":
                        raise ServiceError(
                            "launchctl bootstrap returned EIO and the exact label "
                            "remains absent; bootstrap did not establish the owned job"
                        )
                    raise ServiceError(
                        "launchctl bootstrap returned EIO but the loaded label is "
                        "foreign or unknown; no further launchd mutation was attempted"
                    )
                raise ServiceError(
                    f"launchctl {' '.join(arguments)} failed: "
                    f"{result.stderr.strip()}"
                )
        failure_prefix = "launchd service failed post-bootstrap verification"
        started = service_status(ownership_path, label=label, runner=runner)
        if started["status"] != "job_loaded" or not started["owned"]:
            raise ServiceError("launchd job did not reach the exact owned job state")
        if started.get("environment_drift") is not False:
            raise ServiceError(
                "launchd job reached the owned definition with environment drift"
            )
        same_port_readiness: dict[str, Any] | None = None
        if same_port_preflight is not None:
            same_port_readiness = _wait_for_same_port_ready(
                ownership_path,
                config,
                label=label,
                runner=runner,
            )
        return {
            **started,
            "action": "started",
            "mcp_readiness": same_port_readiness or started["mcp_readiness"],
            "bootstrap_outcome": bootstrap_outcome,
            "bootstrap_preflight": current,
            "bootstrap_eio_recheck": bootstrap_eio_recheck,
            **(
                {"same_port_preflight": same_port_preflight}
                if same_port_preflight is not None
                else {}
            ),
        }
    except BaseException as exc:
        # Every instruction after the first state-changing launchctl call stays
        # inside this boundary, including result inspection and loop transitions.
        compensation = _safe_compensate_bootstrap(
            ownership_path,
            label=label,
            runner=runner,
        )
        if isinstance(exc, ServiceError):
            raise ServiceError(
                f"{failure_prefix}: {exc}; {compensation}"
            ) from exc
        add_note = getattr(exc, "add_note", None)
        if callable(add_note):
            add_note(
                f"{failure_prefix}; {compensation}"
            )
        raise


def service_stop(
    ownership_path: Path,
    *,
    label: str = LAUNCHD_LABEL,
    runner: Runner | None = None,
) -> dict[str, Any]:
    if runner is None:
        runner = subprocess.run
    label = _launchd_label(label)
    _load_ownership(ownership_path, expected_label=label)
    current = service_status(ownership_path, label=label, runner=runner)
    if current["status"] == "stopped":
        return {**current, "action": "noop"}
    if current["status"] != "job_loaded" or not current["owned"]:
        raise ServiceError("refusing to stop a foreign or unknown launchd job")
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["bootout", identity], runner=runner)
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootout failed: {result.stderr.strip()}")
    stopped, stop_wait = _wait_for_service_stopped(
        ownership_path,
        label=label,
        runner=runner,
    )
    return {
        **stopped,
        "action": "stopped",
        "stop_wait": stop_wait,
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
        # The child keeps the authority lock descriptor open.  If the wrapper
        # disappears, a replacement wrapper still cannot start a second writer
        # for this state root while the original server remains alive.
        process = subprocess.Popen(
            [str(server_executable)],
            env=environment,
            start_new_session=True,
            pass_fds=(lock_handle.fileno(),),
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
    render.add_argument("--label", default=LAUNCHD_LABEL)
    foreground_parser = subparsers.add_parser("foreground")
    foreground_parser.add_argument("--server-executable", required=True)
    foreground_parser.add_argument("--env-file", required=True)
    foreground_parser.add_argument("--state-root", required=True)
    for name in ("start", "stop", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--ownership-manifest", required=True)
        command.add_argument("--label", default=LAUNCHD_LABEL)
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
                    label=args.label,
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
            result = service_start(
                Path(args.ownership_manifest),
                label=args.label,
            )
        elif args.command == "stop":
            result = service_stop(
                Path(args.ownership_manifest),
                label=args.label,
            )
        else:
            result = service_status(
                Path(args.ownership_manifest),
                label=args.label,
            )
    except (OSError, ServiceError, subprocess.SubprocessError) as exc:
        print(f"agentstack-mail-service: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
