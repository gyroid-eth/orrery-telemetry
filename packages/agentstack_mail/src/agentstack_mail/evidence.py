"""Candidate-bound producers for pre-cutover machine evidence.

The producers in this module operate only on caller-selected isolated roots and
ports.  They never invoke launchctl, rewrite a client configuration, or touch
the legacy AgentMail endpoint.  A runtime rehearsal must itself execute from
the exact wheel named on the command line and must match a clean candidate
checkout before it can publish terminal receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastmcp import Client

from .contract import COMPATIBILITY_TOOLS


HTTP_RECEIPT_NAME: Final[str] = "http-cli-transport-v1.json"
LIFECYCLE_RECEIPT_NAME: Final[str] = "service-lifecycle-v1.json"
RUNTIME_SCHEMA_VERSION: Final[int] = 1
LEGACY_PORT: Final[int] = 8765
PACKAGE_EVIDENCE_PATH: Final[str] = (
    "packages/agentstack_mail/src/agentstack_mail/evidence.py"
)


class EvidenceError(RuntimeError):
    """A candidate-bound rehearsal could not produce trustworthy evidence."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_terminal(path: Path, value: object) -> None:
    payload = _canonical_json(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _canonical_absolute(path: Path, *, label: str) -> Path:
    raw = os.fspath(path.expanduser())
    absolute = Path(os.path.abspath(raw))
    if not path.is_absolute() or os.path.normpath(raw) != raw or str(absolute) != raw:
        raise EvidenceError(f"{label} must be a canonical absolute path")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError(f"{label} contains a symbolic path component: {current}")
    return absolute


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise EvidenceError(f"git {' '.join(arguments)} failed: {diagnostic}")
    return result


def _candidate_identity(repository: Path, candidate_commit: str) -> dict[str, Any]:
    if len(candidate_commit) != 40 or set(candidate_commit) - set("0123456789abcdef"):
        raise EvidenceError("candidate commit must be one full lowercase SHA-1")
    if not repository.is_dir() or repository.is_symlink():
        raise EvidenceError("candidate repository must be a real directory")
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
    if head != candidate_commit:
        raise EvidenceError("candidate commit must equal the checkout HEAD")
    if _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout:
        raise EvidenceError("candidate repository must be completely clean")
    candidate_source = _git(
        repository,
        "show",
        f"{candidate_commit}:{PACKAGE_EVIDENCE_PATH}",
    ).stdout
    executing_source = Path(__file__).read_bytes()
    if candidate_source != executing_source:
        raise EvidenceError("executing evidence producer differs from the candidate blob")
    return {
        "repository": str(repository),
        "head": head,
        "tracked_and_untracked_worktree_clean": True,
        "evidence_py_sha256": _sha256_bytes(executing_source),
    }


def _verify_running_from_wheel(wheel: Path) -> dict[str, Any]:
    if not wheel.is_file() or wheel.is_symlink() or wheel.suffix != ".whl":
        raise EvidenceError("wheel must be a regular .whl file")
    try:
        distribution = importlib.metadata.distribution("agentstack-mail")
    except importlib.metadata.PackageNotFoundError as exc:
        raise EvidenceError("agentstack-mail is not installed in the executing environment") from exc
    if sys.prefix == sys.base_prefix:
        raise EvidenceError("runtime rehearsal must execute inside an isolated virtualenv")

    compared: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("agentstack_mail/") and not name.endswith("/")
        )
        if "agentstack_mail/evidence.py" not in names:
            raise EvidenceError("candidate wheel does not contain the evidence producer")
        for name in names:
            installed = Path(distribution.locate_file(name))
            if not installed.is_file() or installed.is_symlink():
                raise EvidenceError(f"installed wheel member is absent or unsafe: {name}")
            if installed.read_bytes() != archive.read(name):
                raise EvidenceError(f"installed wheel member differs from wheel: {name}")
            compared.append(name)

        record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise EvidenceError("candidate wheel must contain one RECORD")
        record_rows = list(csv.reader(io.StringIO(archive.read(record_names[0]).decode())))
        if not record_rows or any(len(row) != 3 for row in record_rows):
            raise EvidenceError("candidate wheel RECORD is malformed")

    expected_module = Path(distribution.locate_file("agentstack_mail/evidence.py"))
    if Path(__file__).resolve() != expected_module.resolve():
        raise EvidenceError("evidence producer is not executing from the installed wheel")
    return {
        "path": str(wheel),
        "sha256": _sha256_file(wheel),
        "installed_version": distribution.version,
        "installed_package_file_count": len(compared),
        "installed_package_files_sha256": _sha256_bytes(_canonical_json(compared)),
        "virtualenv": str(Path(sys.prefix).resolve()),
        "python": str(Path(sys.executable).resolve()),
    }


def _listener_fingerprint(port: int) -> dict[str, Any]:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(executable).is_file():
        raise EvidenceError("lsof is required for non-contact legacy listener evidence")
    result = subprocess.run(
        [executable, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode not in {0, 1}:
        raise EvidenceError(f"lsof listener query failed: {result.stderr.strip()}")
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    records = lines[1:] if lines and lines[0].startswith("COMMAND") else lines
    return {
        "method": "lsof-listener-table-no-network-connection",
        "port": port,
        "listener_count": len(records),
        "table_sha256": _sha256_bytes(result.stdout.encode()),
    }


def _listener_process_ids(port: int) -> list[int]:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(executable).is_file():
        raise EvidenceError("lsof is required for exact listener ownership evidence")
    result = subprocess.run(
        [executable, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode not in {0, 1}:
        raise EvidenceError(f"lsof listener owner query failed: {result.stderr.strip()}")
    return sorted({int(value) for value in result.stdout.split()})


def _single_listener_process_id(port: int) -> int:
    listeners = _listener_process_ids(port)
    if len(listeners) != 1:
        raise EvidenceError(
            f"isolated endpoint must have exactly one listener; observed {listeners}"
        )
    return listeners[0]


def _port_accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _require_free_port(port: int) -> None:
    if port == LEGACY_PORT:
        raise EvidenceError("runtime rehearsal refuses legacy port 8765")
    if not 1 <= port <= 65535:
        raise EvidenceError("runtime rehearsal port is outside 1..65535")
    if _port_accepts(port):
        raise EvidenceError(f"runtime rehearsal port is already accepting connections: {port}")


async def _mcp_probe(url: str, project: Path) -> dict[str, Any]:
    async with Client(url, timeout=5, init_timeout=5) as client:
        tools = await client.list_tools()
        await client.call_tool("health_check", {})
        await client.call_tool("ensure_project", {"human_key": str(project)})
    names = sorted(tool.name for tool in tools)
    return {
        "tool_names": names,
        "tool_count": len(names),
        "tool_names_sha256": _sha256_bytes(_canonical_json(names)),
        "health_check": "passed",
        "ensure_project": "passed",
    }


def _wait_ready(
    process: subprocess.Popen[str],
    *,
    url: str,
    project: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    last_error = "endpoint did not answer"
    while time.monotonic() - started < timeout_seconds:
        if process.poll() is not None:
            raise EvidenceError(f"server exited before readiness with rc={process.returncode}")
        try:
            result = asyncio.run(_mcp_probe(url, project))
        except Exception as exc:  # startup races are expected until the deadline
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
            continue
        if set(result["tool_names"]) != COMPATIBILITY_TOOLS or result["tool_count"] != 24:
            raise EvidenceError("running server did not publish the exact 24-tool boundary")
        return {
            **result,
            "bounded_ready_ms": round((time.monotonic() - started) * 1000, 3),
            "deadline_seconds": timeout_seconds,
        }
    raise EvidenceError(f"server readiness deadline expired: {last_error}")


def _wait_closed(port: int, *, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        if not _port_accepts(port):
            return {
                "status": "closed",
                "bounded_closed_ms": round((time.monotonic() - started) * 1000, 3),
                "deadline_seconds": timeout_seconds,
            }
        time.sleep(0.05)
    raise EvidenceError("isolated endpoint remained reachable after shutdown")


def _write_log(path: Path, content: str) -> dict[str, Any]:
    payload = content.encode("utf-8", "replace")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "path": path.name,
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
    }


def _finish_process(
    process: subprocess.Popen[str],
    *,
    output: Path,
    name: str,
    signum: int | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if signum is not None and process.poll() is None:
        process.send_signal(signum)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise EvidenceError(f"{name} did not terminate within the deadline") from exc
    return {
        "exit_code": process.returncode,
        "signal_sent": signal.Signals(signum).name if signum is not None else "none",
        "traceback_absent": "Traceback (most recent call last)" not in stdout + stderr,
        "stdout": _write_log(output / f"{name}.stdout.log", stdout),
        "stderr": _write_log(output / f"{name}.stderr.log", stderr),
    }


def _sqlite_state(database: Path) -> dict[str, Any]:
    family: dict[str, Any] = {}
    for role, path in (
        ("main", database),
        ("wal", database.with_name(database.name + "-wal")),
        ("shm", database.with_name(database.name + "-shm")),
    ):
        if path.is_file() and not path.is_symlink():
            family[role] = {
                "state": "PRESENT",
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        elif not path.exists() and not path.is_symlink():
            family[role] = {"state": "ABSENT"}
        else:
            raise EvidenceError(f"SQLite family member is not a regular file: {path}")
    if family["main"]["state"] != "PRESENT":
        raise EvidenceError("runtime rehearsal did not create the SQLite main file")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    if integrity != ("ok",):
        raise EvidenceError("runtime rehearsal SQLite integrity check failed")
    return {
        "family": family,
        "integrity_check": "ok",
        "journal_mode": str(journal_mode[0]).lower(),
    }


def _clean_environment(env_file: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENTSTACK_MAIL_")
        and not name.startswith("MCP_AGENT_MAIL_")
    }
    environment.update(
        {
            "AGENTSTACK_MAIL_ENV_FILE": str(env_file),
            "PYTHONUNBUFFERED": "1",
            "LC_ALL": "C",
        }
    )
    return environment


def _write_env(path: Path, state_root: Path, port: int, *, mode: str) -> None:
    values = (
        f"AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE={mode}",
        "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1",
        f"AGENTSTACK_MAIL_HTTP_PORT={port}",
        "AGENTSTACK_MAIL_HTTP_PATH=/mcp",
        f"AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///{state_root / 'storage.sqlite3'}",
        f"AGENTSTACK_MAIL_STORAGE_ROOT={state_root / 'archive'}",
        f"AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR={state_root / 'signals'}",
        "AGENTSTACK_MAIL_LOG_RICH_ENABLED=false",
        "AGENTSTACK_MAIL_LOG_JSON_ENABLED=false",
    )
    path.write_text("\n".join(values) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _spawn(arguments: list[str], *, env_file: Path, cwd: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        arguments,
        cwd=cwd,
        env=_clean_environment(env_file),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _spawn_tracked(
    arguments: list[str],
    *,
    env_file: Path,
    cwd: Path,
    processes: list[subprocess.Popen[str]],
) -> subprocess.Popen[str]:
    process = _spawn(arguments, env_file=env_file, cwd=cwd)
    processes.append(process)
    return process


def _cleanup_isolated_runtime(
    processes: list[subprocess.Popen[str]],
    *,
    port: int,
    timeout_seconds: float = 10.0,
) -> None:
    """Best-effort cleanup constrained to processes spawned here and one port."""

    for process in reversed(processes):
        if process.poll() is not None:
            continue
        process.send_signal(signal.SIGTERM)
        try:
            process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and _listener_process_ids(port):
        for process_id in _listener_process_ids(port):
            try:
                os.kill(process_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    for process_id in _listener_process_ids(port):
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if _listener_process_ids(port):
        raise EvidenceError(f"isolated listener cleanup failed for port {port}")


def _negative_entrypoints(
    *,
    output: Path,
    service_executable: Path,
    server_executable: Path,
    port: int,
    processes: list[subprocess.Popen[str]],
) -> list[dict[str, Any]]:
    negative_root = output / "negative-state"
    negative_root.mkdir(mode=0o700)
    env_file = output / "negative.env"
    _write_env(env_file, negative_root, port, mode="coerce")
    commands = (
        ("direct-cli", [str(server_executable)]),
        (
            "service-foreground",
            [
                str(service_executable),
                "foreground",
                "--server-executable",
                str(server_executable),
                "--env-file",
                str(env_file),
                "--state-root",
                str(negative_root),
            ],
        ),
    )
    results: list[dict[str, Any]] = []
    for name, arguments in commands:
        process = _spawn_tracked(
            arguments,
            env_file=env_file,
            cwd=output,
            processes=processes,
        )
        outcome = _finish_process(
            process,
            output=output,
            name=f"negative-{name}",
            timeout_seconds=5,
        )
        if outcome["exit_code"] == 0:
            raise EvidenceError(f"{name} accepted non-passthrough identity mode")
        if _port_accepts(port):
            raise EvidenceError(f"{name} left an endpoint after rejected startup")
        unexpected = sorted(
            path.relative_to(negative_root).as_posix()
            for path in negative_root.rglob("*")
        )
        if unexpected:
            raise EvidenceError(f"{name} left partial runtime state: {unexpected}")
        results.append(
            {
                "entrypoint": name,
                "identity_mode": "coerce",
                "rejected_before_endpoint": True,
                "partial_runtime_paths": [],
                "process": outcome,
            }
        )
    return results


def _run_runtime_rehearsal(
    *,
    output_directory: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
    port: int,
    timeout_seconds: float = 20.0,
    require_legacy_listener: bool = True,
    processes: list[subprocess.Popen[str]],
) -> dict[str, Any]:
    """Run both installed entrypoints and the isolated lifecycle sequence."""

    output_directory = _canonical_absolute(output_directory, label="output directory")
    wheel = _canonical_absolute(wheel, label="wheel")
    candidate_repository = _canonical_absolute(
        candidate_repository, label="candidate repository"
    )
    if output_directory.exists() or output_directory.is_symlink():
        raise EvidenceError(f"output directory must be absent: {output_directory}")
    if not output_directory.parent.is_dir() or output_directory.parent.is_symlink():
        raise EvidenceError("output parent must be a real existing directory")
    if not 1 <= timeout_seconds <= 120:
        raise EvidenceError("timeout_seconds must be in [1, 120]")
    _require_free_port(port)
    candidate = _candidate_identity(candidate_repository, candidate_commit)
    installed_wheel = _verify_running_from_wheel(wheel)

    server_executable = Path(sys.executable).parent / "agentstack-mail"
    service_executable = Path(sys.executable).parent / "agentstack-mail-service"
    for executable in (server_executable, service_executable):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise EvidenceError(f"installed entrypoint is absent: {executable}")

    started_at = datetime.now(UTC).isoformat()
    output_directory.mkdir(mode=0o700)
    marker = output_directory / "runtime-rehearsal.in-progress.json"
    _write_terminal(
        marker,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "candidate_commit": candidate_commit,
            "started_at": started_at,
        },
    )
    os.chmod(marker, 0o600)
    state_root = output_directory / "state"
    state_root.mkdir(mode=0o700)
    (state_root / "archive").mkdir(mode=0o700)
    (state_root / "signals").mkdir(mode=0o700)
    database = state_root / "storage.sqlite3"
    database.touch(mode=0o600)
    project = output_directory / "probe-project"
    project.mkdir(mode=0o700)
    env_file = output_directory / "runtime.env"
    _write_env(env_file, state_root, port, mode="passthrough")
    url = f"http://127.0.0.1:{port}/mcp"
    legacy_before = _listener_fingerprint(LEGACY_PORT)
    if require_legacy_listener and legacy_before["listener_count"] < 1:
        raise EvidenceError("legacy listener was required but not observed")

    direct = _spawn_tracked(
        [str(server_executable)],
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    direct_ready = _wait_ready(
        direct, url=url, project=project, timeout_seconds=timeout_seconds
    )
    direct_process = _finish_process(
        direct,
        output=output_directory,
        name="direct-cli",
        signum=signal.SIGTERM,
        timeout_seconds=timeout_seconds,
    )
    direct_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    if direct_process["exit_code"] != 0 or not direct_process["traceback_absent"]:
        raise EvidenceError("direct CLI did not shut down cleanly after SIGTERM")
    direct_sqlite = _sqlite_state(database)

    service_arguments = [
        str(service_executable),
        "foreground",
        "--server-executable",
        str(server_executable),
        "--env-file",
        str(env_file),
        "--state-root",
        str(state_root),
    ]
    first = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    first_ready = _wait_ready(first, url=url, project=project, timeout_seconds=timeout_seconds)
    first_stop = _finish_process(
        first,
        output=output_directory,
        name="service-normal-stop",
        signum=signal.SIGTERM,
        timeout_seconds=timeout_seconds,
    )
    stopped = _wait_closed(port, timeout_seconds=timeout_seconds)
    if first_stop["exit_code"] != 0 or not first_stop["traceback_absent"]:
        raise EvidenceError("service foreground path did not stop cleanly")

    second = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    second_ready = _wait_ready(second, url=url, project=project, timeout_seconds=timeout_seconds)
    duplicate = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    duplicate_outcome = _finish_process(
        duplicate,
        output=output_directory,
        name="service-duplicate",
        timeout_seconds=5,
    )
    if duplicate_outcome["exit_code"] == 0:
        raise EvidenceError("duplicate service unexpectedly acquired the writer lock")
    duplicate_health = asyncio.run(_mcp_probe(url, project))

    crashed_child_pid = _single_listener_process_id(port)
    try:
        os.killpg(crashed_child_pid, signal.SIGKILL)
    except ProcessLookupError as exc:
        raise EvidenceError("service child vanished before crash injection") from exc
    crash_outcome = _finish_process(
        second,
        output=output_directory,
        name="service-crash",
        timeout_seconds=timeout_seconds,
    )
    crash_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    if crash_outcome["exit_code"] == 0:
        raise EvidenceError("forced child crash unexpectedly produced a normal service exit")
    crash_sqlite = _sqlite_state(database)

    recovered = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    recovered_ready = _wait_ready(
        recovered, url=url, project=project, timeout_seconds=timeout_seconds
    )
    recovery_sqlite = _sqlite_state(database)
    final_stop = _finish_process(
        recovered,
        output=output_directory,
        name="service-recovered-stop",
        signum=signal.SIGTERM,
        timeout_seconds=timeout_seconds,
    )
    final_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    if final_stop["exit_code"] != 0 or not final_stop["traceback_absent"]:
        raise EvidenceError("recovered service did not stop cleanly")

    lost_wrapper = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    lost_wrapper_ready = _wait_ready(
        lost_wrapper,
        url=url,
        project=project,
        timeout_seconds=timeout_seconds,
    )
    inherited_lock_server_pid = _single_listener_process_id(port)
    lost_wrapper.kill()
    wrapper_exit_code = lost_wrapper.wait(timeout=5)
    if wrapper_exit_code != -signal.SIGKILL:
        raise EvidenceError("wrapper-loss injection did not terminate by SIGKILL")
    if _single_listener_process_id(port) != inherited_lock_server_pid:
        raise EvidenceError("server did not survive wrapper-loss injection")

    replacement = _spawn_tracked(
        service_arguments,
        env_file=env_file,
        cwd=output_directory,
        processes=processes,
    )
    replacement_outcome = _finish_process(
        replacement,
        output=output_directory,
        name="service-wrapper-loss-replacement",
        timeout_seconds=5,
    )
    replacement_stderr = (
        output_directory / replacement_outcome["stderr"]["path"]
    ).read_text(encoding="utf-8")
    if (
        replacement_outcome["exit_code"] != 1
        or "another service process owns this state root" not in replacement_stderr
    ):
        raise EvidenceError("replacement acquired authority after wrapper loss")
    wrapper_loss_health = asyncio.run(_mcp_probe(url, project))
    os.kill(inherited_lock_server_pid, signal.SIGTERM)
    wrapper_loss_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    lost_wrapper_outcome = _finish_process(
        lost_wrapper,
        output=output_directory,
        name="service-wrapper-loss",
        timeout_seconds=timeout_seconds,
    )

    negative = _negative_entrypoints(
        output=output_directory,
        service_executable=service_executable,
        server_executable=server_executable,
        port=port,
        processes=processes,
    )
    legacy_after = _listener_fingerprint(LEGACY_PORT)
    if legacy_after != legacy_before:
        raise EvidenceError("legacy listener identity changed during isolated rehearsal")
    completed_at = datetime.now(UTC).isoformat()
    common = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "candidate_commit": candidate_commit,
        "candidate_checkout": candidate,
        "wheel": installed_wheel,
        "started_at": started_at,
        "completed_at": completed_at,
        "isolated_endpoint": url,
        "isolated_state_root": str(state_root),
        "legacy_listener": {
            "required": require_legacy_listener,
            "before": legacy_before,
            "after": legacy_after,
            "network_requests_sent": 0,
        },
    }
    http_receipt = {
        **common,
        "kind": "http-cli-transport",
        "entrypoints": [
            {
                "id": "agentstack-mail",
                "probe": direct_ready,
                "shutdown": direct_process,
                "endpoint_after_shutdown": direct_closed,
                "sqlite_after_shutdown": direct_sqlite,
            },
            {
                "id": "agentstack-mail-service-foreground",
                "probe": first_ready,
                "shutdown": first_stop,
                "endpoint_after_shutdown": stopped,
            },
        ],
        "negative_identity_controls": negative,
        "installer_owned_partial_state": [],
    }
    lifecycle_receipt = {
        **common,
        "kind": "service-lifecycle",
        "controller_mode": "isolated-foreground-launchd-equivalent",
        "sequence": [
            {"step": "start", "status": "ready", "probe": first_ready},
            {"step": "stop", "status": "stopped", "process": first_stop},
            {"step": "status", "status": "stopped", "endpoint": stopped},
            {"step": "start", "status": "ready", "probe": second_ready},
            {
                "step": "duplicate",
                "status": "rejected",
                "process": duplicate_outcome,
                "original_probe": duplicate_health,
            },
            {
                "step": "crash",
                "status": "nonzero_exit",
                "process": crash_outcome,
                "endpoint": crash_closed,
                "sqlite": crash_sqlite,
            },
            {
                "step": "start",
                "status": "recovered_ready",
                "probe": recovered_ready,
                "sqlite": recovery_sqlite,
            },
            {"step": "stop", "status": "stopped", "process": final_stop},
            {"step": "status", "status": "stopped", "endpoint": final_closed},
            {
                "step": "wrapper-loss",
                "status": "original-writer-retained-lock",
                "ready_before_loss": lost_wrapper_ready,
                "wrapper_process": lost_wrapper_outcome,
                "server_pid": inherited_lock_server_pid,
                "replacement": replacement_outcome,
                "original_probe": wrapper_loss_health,
                "endpoint_after_server_stop": wrapper_loss_closed,
                "reproduction_condition": (
                    "wrapper receives SIGKILL after spawning the real server in "
                    "a separate session"
                ),
                "mechanism": "server-child-inherits-authority-lock-fd",
            },
        ],
        "writer_lock_path": str(state_root / "runtime" / "authority.lock"),
        "maximum_observed_ready_services": 1,
    }
    _write_terminal(output_directory / HTTP_RECEIPT_NAME, http_receipt)
    _write_terminal(output_directory / LIFECYCLE_RECEIPT_NAME, lifecycle_receipt)
    marker.unlink()
    directory = os.open(output_directory, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {
        "status": "completed",
        "candidate_commit": candidate_commit,
        "http_receipt": str(output_directory / HTTP_RECEIPT_NAME),
        "http_receipt_sha256": _sha256_file(output_directory / HTTP_RECEIPT_NAME),
        "lifecycle_receipt": str(output_directory / LIFECYCLE_RECEIPT_NAME),
        "lifecycle_receipt_sha256": _sha256_file(
            output_directory / LIFECYCLE_RECEIPT_NAME
        ),
    }


def run_runtime_rehearsal(
    *,
    output_directory: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
    port: int,
    timeout_seconds: float = 20.0,
    require_legacy_listener: bool = True,
) -> dict[str, Any]:
    """Run the rehearsal and guarantee cleanup of its isolated endpoint."""

    processes: list[subprocess.Popen[str]] = []
    completed = False
    try:
        result = _run_runtime_rehearsal(
            output_directory=output_directory,
            wheel=wheel,
            candidate_repository=candidate_repository,
            candidate_commit=candidate_commit,
            port=port,
            timeout_seconds=timeout_seconds,
            require_legacy_listener=require_legacy_listener,
            processes=processes,
        )
        completed = True
        return result
    finally:
        if processes and not completed:
            _cleanup_isolated_runtime(processes, port=port)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentstack-mail-evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    runtime = subparsers.add_parser("runtime-rehearsal")
    runtime.add_argument("--output-dir", required=True)
    runtime.add_argument("--wheel", required=True)
    runtime.add_argument("--candidate-repo", required=True)
    runtime.add_argument("--candidate-commit", required=True)
    runtime.add_argument("--port", type=int, default=18765)
    runtime.add_argument("--timeout-seconds", type=float, default=20.0)
    runtime.add_argument("--allow-missing-legacy-listener", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command != "runtime-rehearsal":  # pragma: no cover - argparse owns it
            raise EvidenceError(f"unsupported evidence command: {args.command}")
        result = run_runtime_rehearsal(
            output_directory=Path(args.output_dir),
            wheel=Path(args.wheel),
            candidate_repository=Path(args.candidate_repo),
            candidate_commit=args.candidate_commit,
            port=args.port,
            timeout_seconds=args.timeout_seconds,
            require_legacy_listener=not args.allow_missing_legacy_listener,
        )
    except (EvidenceError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"agentstack-mail-evidence: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
