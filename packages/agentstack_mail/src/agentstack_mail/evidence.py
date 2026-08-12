"""Candidate-bound producers for pre-cutover machine evidence.

The producers in this module operate only on caller-selected isolated roots and
ports.  The launchd producer may control one explicitly named rehearsal job;
it never rewrites a client configuration or sends a request to the legacy
AgentMail endpoint.  A runtime rehearsal must itself execute from the exact
wheel named on the command line and must match a clean candidate checkout
before it can publish terminal receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import base64
import configparser
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastmcp import Client

from . import restore_acceptance
from . import service as service_runtime
from .contract import COMPATIBILITY_TOOLS


HTTP_RECEIPT_NAME: Final[str] = "http-cli-transport-v1.json"
LIFECYCLE_RECEIPT_NAME: Final[str] = "service-lifecycle-v1.json"
LAUNCHD_RECEIPT_NAME: Final[str] = "service-launchd-lifecycle-v1.json"
RUNTIME_SCHEMA_VERSION: Final[int] = 1
LEGACY_PORT: Final[int] = 8765
LEGACY_LAUNCHD_LABEL: Final[str] = "com.operator.mcp-agent-mail"
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


def _quarantine_terminal(path: Path) -> Path | None:
    """Remove a canonical success name after an unconfirmed publication."""

    if not path.exists() and not path.is_symlink():
        return None
    unconfirmed = path.parent / (
        f".{path.name}.{os.getpid()}.{time.time_ns()}.unconfirmed"
    )
    try:
        os.replace(path, unconfirmed)
    except OSError as exc:
        raise AssertionError(
            f"failed evidence publication retained canonical receipt: {path}"
        ) from exc
    try:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        # The canonical name is already gone. Keep the non-canonical incident
        # artifact even when durability of the secondary quarantine is unknown.
        pass
    return unconfirmed


def _write_terminal(path: Path, value: object) -> str:
    payload = _canonical_json(value)
    digest = _sha256_bytes(payload)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if created or path.exists() or path.is_symlink():
            _quarantine_terminal(path)
        raise
    return digest


def _publish_terminal_set(
    entries: Sequence[tuple[Path, object]],
) -> dict[str, str]:
    """Publish a receipt set or leave no canonical success name on failure."""

    for path, _value in entries:
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
    attempted: list[Path] = []
    digests: dict[str, str] = {}
    try:
        for path, value in entries:
            attempted.append(path)
            digests[str(path)] = _write_terminal(path, value)
    except BaseException:
        for path in reversed(attempted):
            _quarantine_terminal(path)
        raise
    return digests


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


def _verify_running_from_wheel(
    wheel: Path,
    *,
    candidate_repository: Path,
    candidate_commit: str,
) -> dict[str, Any]:
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
        candidate_pyproject = tomllib.loads(
            _git(
                candidate_repository,
                "show",
                f"{candidate_commit}:packages/agentstack_mail/pyproject.toml",
            ).stdout.decode("utf-8")
        )
        names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("agentstack_mail/") and not name.endswith("/")
        )
        source_prefix = "packages/agentstack_mail/src/"
        candidate_member_sources = {
            path.removeprefix(source_prefix): path
            for path in _git(
                candidate_repository,
                "ls-tree",
                "-r",
                "--name-only",
                candidate_commit,
                "--",
                f"{source_prefix}agentstack_mail",
            ).stdout.decode("utf-8").splitlines()
        }
        force_include = (
            candidate_pyproject.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("force-include", {})
        )
        if not isinstance(force_include, dict) or not all(
            isinstance(source, str) and isinstance(destination, str)
            for source, destination in force_include.items()
        ):
            raise EvidenceError("candidate wheel force-include mapping is malformed")
        package_root = "packages/agentstack_mail/"
        for source, destination in force_include.items():
            repository_source = f"{package_root}{source.rstrip('/')}"
            included_paths = _git(
                candidate_repository,
                "ls-tree",
                "-r",
                "--name-only",
                candidate_commit,
                "--",
                repository_source,
            ).stdout.decode("utf-8").splitlines()
            if not included_paths:
                raise EvidenceError(f"candidate force-include source is absent: {source}")
            for included_path in included_paths:
                relative = included_path.removeprefix(repository_source).lstrip("/")
                wheel_name = destination.rstrip("/")
                if relative:
                    wheel_name = f"{wheel_name}/{relative}"
                if wheel_name in candidate_member_sources:
                    raise EvidenceError(f"candidate wheel member collision: {wheel_name}")
                candidate_member_sources[wheel_name] = included_path
        candidate_names = sorted(candidate_member_sources)
        if names != candidate_names:
            raise EvidenceError("wheel package member set differs from candidate Git tree")
        if "agentstack_mail/evidence.py" not in names:
            raise EvidenceError("candidate wheel does not contain the evidence producer")
        for name in names:
            candidate_member = _git(
                candidate_repository,
                "show",
                f"{candidate_commit}:{candidate_member_sources[name]}",
            ).stdout
            if archive.read(name) != candidate_member:
                raise EvidenceError(f"wheel member differs from candidate Git tree: {name}")
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
        wheel_files = {
            name for name in archive.namelist() if name and not name.endswith("/")
        }
        if {row[0] for row in record_rows} != wheel_files:
            raise EvidenceError("candidate wheel RECORD member set is incomplete")
        for member, digest, size in record_rows:
            payload = archive.read(member)
            if member == record_names[0]:
                if digest or size:
                    raise EvidenceError("candidate wheel RECORD self-row must be unhashed")
                continue
            encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
            if digest != f"sha256={encoded.decode()}" or size != str(len(payload)):
                raise EvidenceError(f"candidate wheel RECORD hash mismatch: {member}")

        entry_point_names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entry_point_names) != 1:
            raise EvidenceError("candidate wheel must contain one entry_points.txt")
        entry_points_bytes = archive.read(entry_point_names[0])
        installed_entry_points = distribution.read_text("entry_points.txt")
        if (
            installed_entry_points is None
            or installed_entry_points.encode() != entry_points_bytes
        ):
            raise EvidenceError("installed entry points differ from the wheel")

        expected_scripts = candidate_pyproject.get("project", {}).get("scripts")
        if not isinstance(expected_scripts, dict) or not all(
            isinstance(name, str) and isinstance(target, str)
            for name, target in expected_scripts.items()
        ):
            raise EvidenceError("candidate project.scripts is malformed")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(entry_points_bytes.decode("utf-8"))
        if not parser.has_section("console_scripts"):
            raise EvidenceError("wheel entry_points lacks console_scripts")
        wheel_scripts = dict(parser.items("console_scripts"))
        if wheel_scripts != expected_scripts:
            raise EvidenceError("wheel console scripts differ from candidate pyproject")

    expected_module = Path(distribution.locate_file("agentstack_mail/evidence.py"))
    if Path(__file__).resolve() != expected_module.resolve():
        raise EvidenceError("evidence producer is not executing from the installed wheel")
    return {
        "path": str(wheel),
        "sha256": _sha256_file(wheel),
        "installed_version": distribution.version,
        "installed_package_file_count": len(compared),
        "installed_package_files_sha256": _sha256_bytes(_canonical_json(compared)),
        "candidate_package_members_byte_identical": True,
        "console_scripts": dict(sorted(wheel_scripts.items())),
        "console_scripts_candidate_bound": True,
        "virtualenv": str(Path(sys.prefix).resolve()),
        "python": str(Path(sys.executable).resolve()),
        "python_sha256": _sha256_file(Path(sys.executable).resolve()),
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


def _launchctl(
    arguments: list[str],
    *,
    runner: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("launchctl") or "/bin/launchctl"
    if not Path(executable).is_file():
        raise EvidenceError("launchctl is required for the launchd rehearsal")
    if runner is None:
        runner = subprocess.run
    return runner(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _launchd_job_fingerprint(
    label: str,
    *,
    runner: Any | None = None,
) -> dict[str, Any]:
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["print", identity], runner=runner)
    if result.returncode == 113:
        return {"identity": identity, "state": "absent"}
    if result.returncode != 0:
        raise EvidenceError(
            f"launchd job state is unknown for {identity}: {result.stderr.strip()}"
        )
    path, program, arguments = service_runtime._parse_launchd_record(result.stdout)
    if path is None or program is None or arguments is None:
        raise EvidenceError(f"launchd record is incomplete for {identity}")
    stable_definition = {
        "path": path,
        "program": program,
        "arguments": arguments,
    }
    return {
        "identity": identity,
        "state": "loaded",
        "definition_sha256": _sha256_bytes(_canonical_json(stable_definition)),
    }


def _launchd_job_runtime(
    label: str,
    *,
    runner: Any | None = None,
) -> dict[str, Any]:
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["print", identity], runner=runner)
    if result.returncode != 0:
        raise EvidenceError(f"launchd job is not loaded for ownership proof: {identity}")
    path, program, arguments = service_runtime._parse_launchd_record(result.stdout)
    pid_matches = [
        int(match.group(1))
        for line in result.stdout.splitlines()
        if (match := re.fullmatch(r"\s*pid\s*=\s*([1-9][0-9]*)\s*", line))
        is not None
    ]
    if path is None or program is None or arguments is None or len(pid_matches) != 1:
        raise EvidenceError(f"launchd runtime record is incomplete for {identity}")
    definition = {"path": path, "program": program, "arguments": arguments}
    return {
        "identity": identity,
        "definition_sha256": _sha256_bytes(_canonical_json(definition)),
        "wrapper_pid": pid_matches[0],
    }


def _launchd_definition_snapshot(
    label: str,
    *,
    runner: Any | None = None,
    allow_absent: bool = False,
) -> dict[str, Any]:
    """Capture the loaded definition needed to return to the legacy job."""

    if label != LEGACY_LAUNCHD_LABEL:
        raise EvidenceError("legacy launchd snapshot is restricted to the exact live label")
    identity = f"gui/{os.getuid()}/{label}"
    result = _launchctl(["print", identity], runner=runner)
    if result.returncode == 113 and allow_absent:
        return {"identity": identity, "label": label, "state": "absent"}
    if result.returncode != 0:
        raise EvidenceError(f"legacy launchd job is not loaded: {identity}")
    path_value, program, arguments = service_runtime._parse_launchd_record(result.stdout)
    if path_value is None or program is None or arguments is None:
        raise EvidenceError("legacy launchd record is incomplete")
    plist_path = _canonical_absolute(Path(path_value), label="legacy launchd plist")
    if not plist_path.is_file() or plist_path.is_symlink():
        raise EvidenceError("legacy launchd plist must be a regular file")
    payload = plist_path.read_bytes()
    try:
        definition = plistlib.loads(payload)
    except Exception as exc:
        raise EvidenceError("legacy launchd plist is malformed") from exc
    if (
        not isinstance(definition, dict)
        or definition.get("Label") != label
        or definition.get("ProgramArguments") != arguments
        or not arguments
        or arguments[0] != program
    ):
        raise EvidenceError("loaded legacy job differs from its plist definition")
    keep_alive = definition.get("KeepAlive", False)
    if not isinstance(keep_alive, (bool, dict)):
        raise EvidenceError("legacy launchd KeepAlive has an unexpected type")
    allowed_keys = {
        "EnvironmentVariables",
        "KeepAlive",
        "Label",
        "ProcessType",
        "ProgramArguments",
        "RunAtLoad",
        "StandardErrorPath",
        "StandardOutPath",
        "ThrottleInterval",
        "WorkingDirectory",
    }
    if set(definition) - allowed_keys:
        raise EvidenceError("legacy launchd plist contains unsupported keys")
    environment = definition.get("EnvironmentVariables", {})
    if not isinstance(environment, dict) or set(environment) - {"HOME", "PATH"}:
        raise EvidenceError("legacy launchd plist contains unsealable environment keys")
    return {
        "identity": identity,
        "label": label,
        "state": "loaded",
        "plist_path": str(plist_path),
        "plist_sha256": _sha256_bytes(payload),
        "program": program,
        "program_arguments": arguments,
        "keep_alive": keep_alive,
        "run_at_load": definition.get("RunAtLoad", False),
        "working_directory": definition.get("WorkingDirectory"),
        "plist_bytes": len(payload),
        "plist_bytes_base64": base64.b64encode(payload).decode("ascii"),
        "raw_launchctl_output_retained": False,
        "loaded_path_program_arguments_match_plist": True,
    }


def _process_record(process_id: int) -> dict[str, Any]:
    executable = shutil.which("ps") or "/bin/ps"
    result = subprocess.run(
        [executable, "-ww", "-p", str(process_id), "-o", "ppid=", "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise EvidenceError(f"cannot inspect process {process_id} for ownership proof")
    raw = result.stdout.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        raise EvidenceError(f"process {process_id} has an unparseable ps record")
    try:
        arguments = shlex.split(parts[1])
    except ValueError as exc:
        raise EvidenceError(f"process {process_id} command is unparseable") from exc
    return {"pid": process_id, "ppid": int(parts[0]), "arguments": arguments}


def _legacy_launchd_observation(*, require_loaded: bool) -> dict[str, Any]:
    """Observe one internally consistent live or explicitly offline legacy state."""

    definition = _launchd_definition_snapshot(
        LEGACY_LAUNCHD_LABEL,
        allow_absent=not require_loaded,
    )
    listener = _listener_fingerprint(LEGACY_PORT)
    if not require_loaded:
        if definition["state"] != "absent" or listener["listener_count"] != 0:
            raise EvidenceError(
                "offline legacy observation requires both job and listener absence"
            )
        return {
            "definition": definition,
            "listener": listener,
            "runtime": None,
            "cutover_eligible": False,
            "network_requests_sent": 0,
        }
    if definition["state"] != "loaded" or listener["listener_count"] != 1:
        raise EvidenceError(
            "live legacy observation requires one loaded job and exactly one listener"
        )
    runtime = _launchd_job_runtime(LEGACY_LAUNCHD_LABEL)
    stable_definition = {
        "path": definition["plist_path"],
        "program": definition["program"],
        "arguments": definition["program_arguments"],
    }
    if runtime["definition_sha256"] != _sha256_bytes(
        _canonical_json(stable_definition)
    ):
        raise EvidenceError("legacy launchd runtime changed during the observation")
    listener_pids = _listener_process_ids(LEGACY_PORT)
    if len(listener_pids) != 1:
        raise EvidenceError("legacy endpoint must have exactly one listener PID")
    wrapper = _process_record(int(runtime["wrapper_pid"]))
    listener_process = _process_record(listener_pids[0])
    if wrapper["pid"] != runtime["wrapper_pid"] or listener_process["ppid"] != wrapper["pid"]:
        raise EvidenceError("legacy listener is not a child of the loaded legacy job")
    return {
        "definition": definition,
        "listener": listener,
        "runtime": {
            "identity": runtime["identity"],
            "definition_sha256": runtime["definition_sha256"],
            "wrapper_pid": wrapper["pid"],
            "listener_pid": listener_pids[0],
            "listener_port": LEGACY_PORT,
            "listener_is_wrapper_child": True,
        },
        "cutover_eligible": True,
        "network_requests_sent": 0,
    }


def _owned_launchd_listener(
    *,
    label: str,
    port: int,
    expected_definition_sha256: str,
    service_executable: Path,
    server_executable: Path,
    env_file: Path,
    state_root: Path,
) -> dict[str, Any]:
    runtime = _launchd_job_runtime(label)
    if runtime["definition_sha256"] != expected_definition_sha256:
        raise EvidenceError("loaded launchd definition differs from rendered ownership")
    listener_pid = _single_listener_process_id(port)
    wrapper = _process_record(int(runtime["wrapper_pid"]))
    listener = _process_record(listener_pid)
    required_wrapper_arguments = {
        str(service_executable),
        "foreground",
        str(server_executable),
        str(env_file),
        str(state_root),
    }
    if not required_wrapper_arguments.issubset(set(wrapper["arguments"])):
        raise EvidenceError("launchd wrapper command differs from rendered ownership")
    if listener["ppid"] != wrapper["pid"]:
        raise EvidenceError("isolated listener is not a child of the exact launchd wrapper")
    if str(server_executable) not in listener["arguments"]:
        raise EvidenceError("isolated listener did not execute the verified server shim")
    return {
        "listener_pid": listener_pid,
        "wrapper_pid": wrapper["pid"],
        "definition_sha256": runtime["definition_sha256"],
        "listener_is_exact_wrapper_child": True,
        "verified_server_shim": str(server_executable),
    }


def _disabled_override_snapshot(
    label: str,
    *,
    runner: Any | None = None,
) -> dict[str, Any]:
    result = _launchctl(["print-disabled", f"gui/{os.getuid()}"], runner=runner)
    if result.returncode != 0:
        raise EvidenceError(
            f"launchctl print-disabled failed: {result.stderr.strip()}"
        )
    if "disabled services = {" not in result.stdout:
        raise EvidenceError("launchctl print-disabled returned an unknown format")
    matches: list[bool] = []
    pattern = re.compile(
        r'^\s*"(?P<label>[^"]+)"\s*=>\s*'
        r'(?P<value>true|false|enabled|disabled)\s*$'
    )
    for line in result.stdout.splitlines():
        parsed = pattern.fullmatch(line)
        if label in line and (parsed is None or parsed.group("label") != label):
            raise EvidenceError(
                "launchctl print-disabled has a malformed exact-label entry"
            )
        if parsed is not None and parsed.group("label") == label:
            matches.append(parsed.group("value") in {"true", "disabled"})
    if len(matches) > 1:
        raise EvidenceError("launchctl print-disabled repeated the rehearsal label")
    return {
        "method": "launchctl-print-disabled-exact-label-only",
        "label": label,
        "entry_present": bool(matches),
        "disabled": matches[0] if matches else None,
        "raw_domain_output_retained": False,
    }


def _require_disabled_override_before(snapshot: dict[str, Any]) -> None:
    if snapshot.get("entry_present") and snapshot.get("disabled") is not False:
        raise EvidenceError(
            "rehearsal label is persistently disabled before bootstrap; refusing a "
            "controller path that cannot match the authorized order"
        )


def _require_disabled_override_after(snapshot: dict[str, Any]) -> None:
    if snapshot.get("entry_present") is not True or snapshot.get("disabled") is not False:
        raise EvidenceError(
            "launchd enable did not leave an explicit enabled override for the label"
        )


def _ensure_rehearsal_job_absent(
    label: str,
    *,
    expected_definition_sha256: str,
    runner: Any | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    label = service_runtime.rehearsal_launchd_label(label)
    if timeout_seconds <= 0:
        raise EvidenceError("launchd cleanup timeout must be positive")
    before = _launchd_job_fingerprint(label, runner=runner)
    bootout: dict[str, Any] | None = None
    if before["state"] == "loaded":
        if before.get("definition_sha256") != expected_definition_sha256:
            raise EvidenceError(
                "refusing cleanup of a rehearsal label with foreign ownership"
            )
        result = _launchctl(
            ["bootout", f"gui/{os.getuid()}/{label}"],
            runner=runner,
        )
        bootout = {
            "exit_code": result.returncode,
            "stdout_sha256": _sha256_bytes(result.stdout.encode()),
            "stderr_sha256": _sha256_bytes(result.stderr.encode()),
        }
        if result.returncode != 0:
            raise EvidenceError(
                f"rehearsal cleanup bootout failed: {result.stderr.strip()}"
            )
    wait_started = time.monotonic()
    polls = 0
    while True:
        remaining = timeout_seconds - (time.monotonic() - wait_started)
        if remaining <= 0:
            raise EvidenceError(
                "rehearsal launchd identity did not retire before cleanup deadline"
            )

        def bounded_runner(
            arguments: list[str],
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            kwargs["timeout"] = min(float(kwargs.get("timeout", remaining)), remaining)
            if runner is None:
                return subprocess.run(arguments, **kwargs)
            return runner(arguments, **kwargs)

        polls += 1
        after = _launchd_job_fingerprint(label, runner=bounded_runner)
        elapsed = time.monotonic() - wait_started
        if elapsed > timeout_seconds:
            raise EvidenceError(
                "rehearsal launchd identity did not retire before cleanup deadline"
            )
        if after["state"] == "absent":
            break
        if after.get("definition_sha256") != expected_definition_sha256:
            raise EvidenceError(
                "rehearsal label changed ownership while waiting for bootout"
            )
        time.sleep(0.05)
    return {
        "before": before,
        "bootout": bootout,
        "after": after,
        "retire_wait": {
            "poll_count": polls,
            "bounded_absent_ms": round(elapsed * 1000, 3),
            "deadline_seconds": timeout_seconds,
        },
    }


def _candidate_rehearsal_label(label: str, candidate_commit: str) -> str:
    """Bind the one mutable launchd identity to the exact candidate SHA."""

    if len(candidate_commit) != 40 or set(candidate_commit) - set("0123456789abcdef"):
        raise EvidenceError("candidate commit must be one full lowercase SHA-1")
    label = service_runtime.rehearsal_launchd_label(label)
    expected_prefix = (
        f"{service_runtime.LAUNCHD_REHEARSAL_PREFIX}{candidate_commit[:8]}."
    )
    if not label.startswith(expected_prefix):
        raise EvidenceError(
            "rehearsal launchd label must contain the exact candidate8 prefix"
        )
    return label


def _foreground_receipt_identity(
    path: Path,
    *,
    candidate_commit: str,
    expected_sha256: str,
    wheel_sha256: str,
    require_legacy_listener: bool,
) -> dict[str, Any]:
    path = _canonical_absolute(path, label="foreground lifecycle receipt")
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("foreground lifecycle receipt must be a regular file")
    if (path.parent / "runtime-rehearsal.in-progress.json").exists() or (
        path.parent / "runtime-rehearsal.in-progress.json"
    ).is_symlink():
        raise EvidenceError("foreground lifecycle receipt still has an in-progress marker")
    if stat.S_IMODE(path.stat().st_mode) != 0o400:
        raise EvidenceError("foreground lifecycle receipt must have mode 0400")
    if len(expected_sha256) != 64 or set(expected_sha256) - set("0123456789abcdef"):
        raise EvidenceError("foreground receipt SHA-256 pin must be lowercase hexadecimal")
    observed_sha256 = _sha256_file(path)
    if observed_sha256 != expected_sha256:
        raise EvidenceError("foreground lifecycle receipt differs from its SHA-256 pin")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read foreground lifecycle receipt: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or value.get("kind") != "service-lifecycle"
        or value.get("candidate_commit") != candidate_commit
    ):
        raise EvidenceError("foreground lifecycle receipt has the wrong identity")
    candidate = value.get("candidate_checkout")
    wheel = value.get("wheel")
    legacy = value.get("legacy_listener")
    if (
        not isinstance(candidate, dict)
        or candidate.get("head") != candidate_commit
        or candidate.get("tracked_and_untracked_worktree_clean") is not True
        or not isinstance(wheel, dict)
        or wheel.get("sha256") != wheel_sha256
        or wheel.get("candidate_package_members_byte_identical") is not True
        or wheel.get("console_scripts_candidate_bound") is not True
        or value.get("maximum_observed_ready_services") != 1
        or not isinstance(legacy, dict)
        or legacy.get("required") is not require_legacy_listener
        or legacy.get("network_requests_sent") != 0
        or legacy.get("before") != legacy.get("after")
    ):
        raise EvidenceError("foreground lifecycle receipt lacks candidate-bound invariants")
    steps = value.get("sequence")
    expected_steps = (
        ("start", "ready"),
        ("stop", "stopped"),
        ("status", "stopped"),
        ("start", "ready"),
        ("duplicate", "rejected"),
        ("crash", "nonzero_exit"),
        ("start", "recovered_ready"),
        ("stop", "stopped"),
        ("status", "stopped"),
        ("wrapper-loss", "original-writer-retained-lock"),
    )
    observed_steps = (
        tuple((step.get("step"), step.get("status")) for step in steps)
        if isinstance(steps, list) and all(isinstance(step, dict) for step in steps)
        else ()
    )
    if observed_steps != expected_steps:
        raise EvidenceError("foreground lifecycle receipt has an incomplete sequence")
    return {
        "path": str(path),
        "sha256": observed_sha256,
        "expected_sha256": expected_sha256,
        "candidate_commit": candidate_commit,
        "wheel_sha256": wheel_sha256,
        "kind": "service-lifecycle",
        "full_sequence_validated": True,
    }


def _ownership_definition_sha256(ownership_path: Path, *, label: str) -> str:
    ownership = service_runtime._load_ownership(
        ownership_path,
        expected_label=label,
    )
    arguments = list(ownership["_expected_arguments"])
    definition = {
        "path": ownership["_artifact_path"],
        "program": arguments[0],
        "arguments": arguments,
    }
    return _sha256_bytes(_canonical_json(definition))


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
        and not name.startswith("PYTHON")
    }
    environment.update(
        {
            "AGENTSTACK_MAIL_ENV_FILE": str(env_file),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
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


def _write_verified_entrypoint_shims(output_directory: Path) -> dict[str, Any]:
    """Create immutable shims that enter candidate-bound installed modules."""

    interpreter = str(Path(sys.executable).resolve())
    if any(character.isspace() for character in interpreter):
        raise EvidenceError("verified Python path cannot contain whitespace")
    installed_root = Path(__file__).resolve().parent.parent
    if not installed_root.is_dir() or installed_root.is_symlink():
        raise EvidenceError("installed distribution root must be a real directory")
    shim_directory = output_directory / "verified-entrypoints"
    shim_directory.mkdir(mode=0o700)
    os.chmod(shim_directory, 0o700)
    definitions = {
        "server": ("agentstack-mail", "agentstack_mail.cli"),
        "service": ("agentstack-mail-service", "agentstack_mail.service"),
    }
    result: dict[str, Any] = {}
    for role, (filename, module) in definitions.items():
        path = shim_directory / filename
        payload = (
            f"#!{interpreter} -I\n"
            "import sys\n"
            f"sys.path.insert(0, {str(installed_root)!r})\n"
            f"from {module} import main\n"
            "main()\n"
        ).encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o500)
        result[role] = {
            "path": str(path),
            "module": module,
            "callable": "main",
            "sha256": _sha256_bytes(payload),
            "mode": "0500",
            "interpreter": interpreter,
            "interpreter_isolated_mode": True,
            "candidate_bound_import_root": str(installed_root),
        }
    directory = os.open(shim_directory, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


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
    installed_wheel = _verify_running_from_wheel(
        wheel,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
    )

    started_at = datetime.now(UTC).isoformat()
    output_directory.mkdir(mode=0o700)
    os.chmod(output_directory, 0o700)
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
    verified_entrypoints = _write_verified_entrypoint_shims(output_directory)
    server_executable = Path(verified_entrypoints["server"]["path"])
    service_executable = Path(verified_entrypoints["service"]["path"])
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
        "verified_entrypoints": verified_entrypoints,
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
    marker.unlink()
    directory = os.open(output_directory, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    http_path = output_directory / HTTP_RECEIPT_NAME
    lifecycle_path = output_directory / LIFECYCLE_RECEIPT_NAME
    terminal_sha256 = _publish_terminal_set(
        ((http_path, http_receipt), (lifecycle_path, lifecycle_receipt))
    )
    return {
        "status": "completed",
        "candidate_commit": candidate_commit,
        "http_receipt": str(http_path),
        "http_receipt_sha256": terminal_sha256[str(http_path)],
        "lifecycle_receipt": str(lifecycle_path),
        "lifecycle_receipt_sha256": terminal_sha256[str(lifecycle_path)],
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


def _service_command(
    service_executable: Path,
    arguments: list[str],
    *,
    env_file: Path,
    output_directory: Path,
    name: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    result = subprocess.run(
        [str(service_executable), *arguments],
        cwd=output_directory,
        env=_clean_environment(env_file),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    process = {
        "exit_code": result.returncode,
        "stdout": _write_log(
            output_directory / f"{name}.stdout.log", result.stdout
        ),
        "stderr": _write_log(
            output_directory / f"{name}.stderr.log", result.stderr
        ),
    }
    if result.returncode != 0:
        raise EvidenceError(
            f"agentstack-mail-service {' '.join(arguments[:1])} failed; "
            f"see {name}.stderr.log"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError(
            f"agentstack-mail-service {' '.join(arguments[:1])} returned non-JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceError("agentstack-mail-service result must be a JSON object")
    return {"result": payload, "process": process}


def _wait_launchd_ready(
    *,
    url: str,
    project: Path,
    port: int,
    label: str,
    expected_definition_sha256: str,
    service_executable: Path,
    server_executable: Path,
    env_file: Path,
    state_root: Path,
    timeout_seconds: float,
    previous_listener_pid: int | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    last_error = "endpoint did not answer"
    while time.monotonic() - started < timeout_seconds:
        try:
            ownership = _owned_launchd_listener(
                label=label,
                port=port,
                expected_definition_sha256=expected_definition_sha256,
                service_executable=service_executable,
                server_executable=server_executable,
                env_file=env_file,
                state_root=state_root,
            )
            listener_pid = int(ownership["listener_pid"])
            if listener_pid == previous_listener_pid:
                raise EvidenceError("launchd has not replaced the crashed listener yet")
            probe = asyncio.run(_mcp_probe(url, project))
        except Exception as exc:  # launchd startup and KeepAlive races are expected
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
            continue
        if set(probe["tool_names"]) != COMPATIBILITY_TOOLS or probe["tool_count"] != 24:
            raise EvidenceError("launchd server did not publish the exact 24-tool boundary")
        return {
            **probe,
            "ownership": ownership,
            "listener_pid": listener_pid,
            "bounded_ready_ms": round((time.monotonic() - started) * 1000, 3),
            "deadline_seconds": timeout_seconds,
        }
    raise EvidenceError(f"launchd readiness deadline expired: {last_error}")


def _launchd_runtime_logs(state_root: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for stream in ("stdout", "stderr"):
        path = state_root / "runtime" / f"service.{stream}.log"
        if not path.is_file() or path.is_symlink():
            raise EvidenceError(f"launchd service {stream} log is absent or unsafe")
        payload = path.read_bytes()
        results[stream] = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": _sha256_bytes(payload),
            "traceback_absent": b"Traceback (most recent call last)" not in payload,
        }
    if not all(value["traceback_absent"] for value in results.values()):
        raise EvidenceError("launchd service log contains a Python traceback")
    return results


def _execute_with_launchd_cleanup(
    *,
    action: Any,
    cleanup: Any,
    label: str,
    port: int,
    output_directory: Path,
) -> tuple[Any, Any]:
    """Make exact-label cleanup unavoidable for Python-level interruptions."""

    previous_handlers: dict[int, Any] = {}
    deferred_signals: set[int] = set()

    def interrupted(signum: int, _frame: Any) -> None:
        raise EvidenceError(
            f"launchd rehearsal interrupted by {signal.Signals(signum).name}"
        )

    handled_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    for signum in handled_signals:
        previous_handlers[signum] = signal.signal(signum, interrupted)

    action_result: Any = None
    action_error: BaseException | None = None
    cleanup_result: Any = None
    cleanup_error: BaseException | None = None
    cleanup_signal_mask: set[signal.Signals] | None = None
    try:
        try:
            action_result = action()
        except BaseException as exc:
            action_error = exc
        finally:
            # A second terminal signal must not interrupt the only authorized
            # cleanup attempt. Record it and fail after cleanup instead of losing it.
            def defer(signum: int, _frame: Any) -> None:
                deferred_signals.add(signum)

            for signum in previous_handlers:
                signal.signal(signum, defer)
            cleanup_signal_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                handled_signals,
            )
            try:
                cleanup_result = cleanup()
            except BaseException as exc:
                cleanup_error = exc
            finally:
                # Cleanup subprocesses inherit the blocked mask, so a terminal
                # process-group signal cannot kill bootout midway. Unblocking
                # here delivers any pending signal to ``defer`` before success.
                signal.pthread_sigmask(signal.SIG_SETMASK, cleanup_signal_mask)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if cleanup_error is not None:
        listeners = _listener_process_ids(port)
        raise EvidenceError(
            "launchd rehearsal cleanup failed; no terminal receipt was published; "
            f"label={label} port={port} listener_pids={listeners} "
            f"output={output_directory}: {cleanup_error}"
        ) from cleanup_error
    if deferred_signals and action_error is None:
        names = sorted(signal.Signals(signum).name for signum in deferred_signals)
        action_error = EvidenceError(
            f"launchd rehearsal interrupted during cleanup by {names}"
        )
    if action_error is not None:
        raise action_error
    return action_result, cleanup_result


def _require_controller_state(
    outcome: dict[str, Any],
    *,
    status: str,
    action: str | None,
) -> None:
    result = outcome.get("result")
    if not isinstance(result, dict):
        raise EvidenceError("launchd controller result is absent")
    expected = {"status": status, "owned": True}
    if any(result.get(key) != value for key, value in expected.items()):
        raise EvidenceError(f"launchd controller did not prove exact {status} state")
    if (
        action != "stopped"
        and result.get("environment_drift") is not False
    ) or result.get("environment_drift") is True:
        raise EvidenceError("launchd controller reported environment drift or omitted it")
    if action is not None and result.get("action") != action:
        raise EvidenceError(f"launchd controller did not perform exact {action} action")


def _launchd_mutation_sequence(
    *,
    service_executable: Path,
    server_executable: Path,
    env_file: Path,
    state_root: Path,
    output_directory: Path,
    controller: list[str],
    label: str,
    expected_definition_sha256: str,
    url: str,
    project: Path,
    port: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    sequence: list[dict[str, Any]] = []
    first_start = _service_command(
        service_executable,
        ["start", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-first-start",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(first_start, status="job_loaded", action="started")
    first_ready = _wait_launchd_ready(
        url=url,
        project=project,
        port=port,
        label=label,
        expected_definition_sha256=expected_definition_sha256,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=state_root,
        timeout_seconds=timeout_seconds,
    )
    sequence.append(
        {
            "step": "start",
            "status": "job_loaded",
            "controller": first_start,
            "probe": first_ready,
        }
    )

    first_stop = _service_command(
        service_executable,
        ["stop", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-first-stop",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(first_stop, status="stopped", action="stopped")
    first_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    stopped_status = _service_command(
        service_executable,
        ["status", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-status-after-first-stop",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(stopped_status, status="stopped", action=None)
    sequence.append(
        {
            "step": "stop",
            "status": "stopped",
            "controller": first_stop,
            "endpoint": first_closed,
            "status_probe": stopped_status,
        }
    )

    second_start = _service_command(
        service_executable,
        ["start", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-second-start",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(second_start, status="job_loaded", action="started")
    second_ready = _wait_launchd_ready(
        url=url,
        project=project,
        port=port,
        label=label,
        expected_definition_sha256=expected_definition_sha256,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=state_root,
        timeout_seconds=timeout_seconds,
    )
    crashed_pid = int(second_ready["listener_pid"])
    sequence.append(
        {
            "step": "start",
            "status": "job_loaded",
            "controller": second_start,
            "probe": second_ready,
        }
    )

    ownership_recheck = _owned_launchd_listener(
        label=label,
        port=port,
        expected_definition_sha256=expected_definition_sha256,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=state_root,
    )
    if ownership_recheck["listener_pid"] != crashed_pid:
        raise EvidenceError("listener ownership changed before crash injection")
    try:
        os.kill(crashed_pid, signal.SIGKILL)
    except ProcessLookupError as exc:
        raise EvidenceError("launchd listener vanished before crash injection") from exc

    recovered_ready = _wait_launchd_ready(
        url=url,
        project=project,
        port=port,
        label=label,
        expected_definition_sha256=expected_definition_sha256,
        service_executable=service_executable,
        server_executable=server_executable,
        env_file=env_file,
        state_root=state_root,
        timeout_seconds=timeout_seconds,
        previous_listener_pid=crashed_pid,
    )
    recovered_status = _service_command(
        service_executable,
        ["status", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-status-after-keepalive",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(recovered_status, status="job_loaded", action=None)
    sequence.append(
        {
            "step": "crash",
            "status": "keepalive-recovered-ready",
            "injected_signal": "SIGKILL",
            "ownership_rechecked_before_signal": ownership_recheck,
            "crashed_listener_pid": crashed_pid,
            "replacement_listener_pid": recovered_ready["listener_pid"],
            "different_listener_pid": recovered_ready["listener_pid"] != crashed_pid,
            "probe": recovered_ready,
            "status_probe": recovered_status,
        }
    )

    final_stop = _service_command(
        service_executable,
        ["stop", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-final-stop",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(final_stop, status="stopped", action="stopped")
    final_closed = _wait_closed(port, timeout_seconds=timeout_seconds)
    final_status = _service_command(
        service_executable,
        ["status", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-status-after-final-stop",
        timeout_seconds=timeout_seconds,
    )
    _require_controller_state(final_status, status="stopped", action=None)
    sequence.append(
        {
            "step": "stop",
            "status": "stopped",
            "controller": final_stop,
            "endpoint": final_closed,
            "status_probe": final_status,
        }
    )
    return sequence


def _run_launchd_rehearsal(
    *,
    output_directory: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
    foreground_receipt: Path,
    foreground_receipt_sha256: str,
    label: str,
    port: int,
    timeout_seconds: float,
    require_legacy_listener: bool,
) -> dict[str, Any]:
    output_directory = _canonical_absolute(output_directory, label="output directory")
    wheel = _canonical_absolute(wheel, label="wheel")
    candidate_repository = _canonical_absolute(
        candidate_repository, label="candidate repository"
    )
    if output_directory.exists() or output_directory.is_symlink():
        raise EvidenceError(f"output directory must be absent: {output_directory}")
    if not output_directory.parent.is_dir() or output_directory.parent.is_symlink():
        raise EvidenceError("output parent must be a real existing directory")
    if not 25 <= timeout_seconds <= 120:
        raise EvidenceError("timeout_seconds must be in [25, 120]")
    _require_free_port(port)
    candidate = _candidate_identity(candidate_repository, candidate_commit)
    installed_wheel = _verify_running_from_wheel(
        wheel,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
    )
    label = _candidate_rehearsal_label(label, candidate_commit)
    foreground = _foreground_receipt_identity(
        foreground_receipt,
        candidate_commit=candidate_commit,
        expected_sha256=foreground_receipt_sha256,
        wheel_sha256=str(installed_wheel["sha256"]),
        require_legacy_listener=require_legacy_listener,
    )

    started_at = datetime.now(UTC).isoformat()
    output_directory.mkdir(mode=0o700)
    os.chmod(output_directory, 0o700)
    marker = output_directory / "launchd-rehearsal.in-progress.json"
    _write_terminal(
        marker,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "candidate_commit": candidate_commit,
            "label": label,
            "port": port,
            "started_at": started_at,
        },
    )
    os.chmod(marker, 0o600)
    verified_entrypoints = _write_verified_entrypoint_shims(output_directory)
    service_executable = Path(verified_entrypoints["service"]["path"])
    server_executable = Path(verified_entrypoints["server"]["path"])

    state_root = output_directory / "state"
    state_root.mkdir(mode=0o700)
    os.chmod(state_root, 0o700)
    for child in ("archive", "signals", "runtime"):
        (state_root / child).mkdir(mode=0o700)
        os.chmod(state_root / child, 0o700)
    database = state_root / "storage.sqlite3"
    database.touch(mode=0o600)
    project = output_directory / "probe-project"
    project.mkdir(mode=0o700)
    os.chmod(project, 0o700)
    env_file = output_directory / "runtime.env"
    _write_env(env_file, state_root, port, mode="passthrough")
    url = f"http://127.0.0.1:{port}/mcp"

    legacy_observation_before = _legacy_launchd_observation(
        require_loaded=require_legacy_listener,
    )
    legacy_before = legacy_observation_before["listener"]
    legacy_launchd_before = legacy_observation_before["definition"]
    production_before = _launchd_job_fingerprint(service_runtime.LAUNCHD_LABEL)
    disabled_before = _disabled_override_snapshot(label)
    rehearsal_before = _launchd_job_fingerprint(label)
    if rehearsal_before["state"] != "absent":
        raise EvidenceError("rehearsal launchd identity existed before the rehearsal")

    render = _service_command(
        service_executable,
        [
            "render",
            "--output-dir",
            str(output_directory / "artifacts"),
            "--service-executable",
            str(service_executable),
            "--server-executable",
            str(server_executable),
            "--env-file",
            str(env_file),
            "--state-root",
            str(state_root),
            "--label",
            label,
        ],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-render",
        timeout_seconds=timeout_seconds,
    )
    ownership = Path(str(render["result"].get("ownership_manifest", "")))
    if not ownership.is_file() or ownership.is_symlink():
        raise EvidenceError("launchd render did not produce a safe ownership manifest")
    controller = ["--ownership-manifest", str(ownership), "--label", label]
    initial_status = _service_command(
        service_executable,
        ["status", *controller],
        env_file=env_file,
        output_directory=output_directory,
        name="launchd-status-before",
        timeout_seconds=timeout_seconds,
    )
    if initial_status["result"].get("status") != "stopped":
        raise EvidenceError("rehearsal controller did not observe the initial stopped state")

    _require_controller_state(initial_status, status="stopped", action=None)
    expected_definition_sha256 = _ownership_definition_sha256(
        ownership,
        label=label,
    )
    _require_disabled_override_before(disabled_before)

    def mutate() -> list[dict[str, Any]]:
        return _launchd_mutation_sequence(
            service_executable=service_executable,
            server_executable=server_executable,
            env_file=env_file,
            state_root=state_root,
            output_directory=output_directory,
            controller=controller,
            label=label,
            expected_definition_sha256=expected_definition_sha256,
            url=url,
            project=project,
            port=port,
            timeout_seconds=timeout_seconds,
        )

    def cleanup_exact_label() -> dict[str, Any]:
        launchd_cleanup = _ensure_rehearsal_job_absent(
            label,
            expected_definition_sha256=expected_definition_sha256,
            timeout_seconds=timeout_seconds,
        )
        endpoint = _wait_closed(port, timeout_seconds=timeout_seconds)
        remaining_pids = _listener_process_ids(port)
        if remaining_pids:
            raise EvidenceError(
                f"bootout left listener PIDs {remaining_pids} on port {port}; "
                "automatic signal cleanup is forbidden"
            )
        return {
            "launchd": launchd_cleanup,
            "endpoint": endpoint,
            "remaining_listener_pids": [],
        }

    sequence, cleanup_result = _execute_with_launchd_cleanup(
        action=mutate,
        cleanup=cleanup_exact_label,
        label=label,
        port=port,
        output_directory=output_directory,
    )
    cleanup = cleanup_result["launchd"]
    cleanup_endpoint = cleanup_result["endpoint"]
    disabled_after = _disabled_override_snapshot(label)
    _require_disabled_override_after(disabled_after)
    production_after = _launchd_job_fingerprint(service_runtime.LAUNCHD_LABEL)
    legacy_observation_after = _legacy_launchd_observation(
        require_loaded=require_legacy_listener,
    )
    legacy_launchd_after = legacy_observation_after["definition"]
    legacy_after = legacy_observation_after["listener"]
    if production_after != production_before:
        raise EvidenceError("production launchd label changed during rehearsal")
    if legacy_after != legacy_before:
        raise EvidenceError("legacy listener identity changed during launchd rehearsal")
    if legacy_launchd_after != legacy_launchd_before:
        raise EvidenceError("live legacy launchd definition changed during rehearsal")
    if legacy_observation_after != legacy_observation_before:
        raise EvidenceError("legacy launchd topology changed during rehearsal")
    if _launchd_job_fingerprint(label)["state"] != "absent":
        raise EvidenceError("rehearsal label was not absent at terminal publication")
    runtime_logs = _launchd_runtime_logs(state_root)
    sqlite = _sqlite_state(database)

    receipt = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": "service-launchd-lifecycle",
        "candidate_commit": candidate_commit,
        "candidate_checkout": candidate,
        "wheel": installed_wheel,
        "verified_entrypoints": verified_entrypoints,
        "foreground_receipt": foreground,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "label": label,
        "isolated_endpoint": url,
        "isolated_state_root": str(state_root),
        "render": render,
        "initial_status": initial_status,
        "sequence": sequence,
        "cleanup": {
            "launchd": cleanup,
            "endpoint": cleanup_endpoint,
            "remaining_listener_pids": [],
            "terminal_receipt_published_only_after_cleanup": True,
        },
        "launchd_disabled_override": {
            "before": disabled_before,
            "after": disabled_after,
            "outside_isolated_filesystem": True,
            "cleanup_action": "retained-as-observed; disable was not authorized",
        },
        "production_launchd_label": {
            "before": production_before,
            "after": production_after,
            "unchanged": True,
        },
        "legacy_launchd_label": {
            "before": legacy_launchd_before,
            "after": legacy_launchd_after,
            "unchanged": True,
        },
        "legacy_runtime_topology": {
            "before": legacy_observation_before["runtime"],
            "after": legacy_observation_after["runtime"],
            "unchanged": True,
        },
        "legacy_listener": {
            "required": require_legacy_listener,
            "before": legacy_before,
            "after": legacy_after,
            "network_requests_sent": 0,
            "unchanged": True,
        },
        "cutover_eligible": require_legacy_listener,
        "sqlite_after_cleanup": sqlite,
        "runtime_logs_after_cleanup": runtime_logs,
        "foreground_comparison": {
            "same_candidate": True,
            "foreground_controller_state": "endpoint-only stopped/start observations",
            "launchd_controller_state": "job_loaded/stopped observations",
            "foreground_crash_recovery": "explicit fresh foreground start",
            "launchd_crash_recovery": "automatic KeepAlive replacement listener",
            "different_listener_pid_after_keepalive": True,
        },
        "maximum_observed_ready_services": 1,
    }
    receipt_path = output_directory / LAUNCHD_RECEIPT_NAME
    marker.unlink()
    directory = os.open(output_directory, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    receipt_sha256 = _write_terminal(receipt_path, receipt)
    return {
        "status": "completed",
        "candidate_commit": candidate_commit,
        "label": label,
        "receipt": str(receipt_path),
        "receipt_sha256": receipt_sha256,
    }


def run_launchd_rehearsal(
    *,
    output_directory: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
    foreground_receipt: Path,
    foreground_receipt_sha256: str,
    label: str,
    port: int,
    timeout_seconds: float = 30.0,
    require_legacy_listener: bool = True,
) -> dict[str, Any]:
    """Run one candidate-bound launchd rehearsal on one isolated identity."""

    return _run_launchd_rehearsal(
        output_directory=output_directory,
        wheel=wheel,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
        foreground_receipt=foreground_receipt,
        foreground_receipt_sha256=foreground_receipt_sha256,
        label=label,
        port=port,
        timeout_seconds=timeout_seconds,
        require_legacy_listener=require_legacy_listener,
    )


def write_legacy_launchd_snapshot(
    *,
    output_path: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
) -> dict[str, Any]:
    """Seal the read-only definition and process topology of the live legacy job."""

    output_path = _canonical_absolute(output_path, label="legacy snapshot output")
    if output_path.exists() or output_path.is_symlink():
        raise EvidenceError(f"legacy snapshot output must be absent: {output_path}")
    if not output_path.parent.is_dir() or output_path.parent.is_symlink():
        raise EvidenceError("legacy snapshot output parent must be a real directory")
    wheel = _canonical_absolute(wheel, label="wheel")
    candidate_repository = _canonical_absolute(
        candidate_repository,
        label="candidate repository",
    )
    candidate = _candidate_identity(candidate_repository, candidate_commit)
    installed_wheel = _verify_running_from_wheel(
        wheel,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
    )
    observation = _legacy_launchd_observation(require_loaded=True)
    definition = observation["definition"]
    runtime = observation["runtime"]
    if not isinstance(runtime, dict):  # pragma: no cover - live helper invariant
        raise EvidenceError("legacy launchd runtime observation is missing")
    new_candidate_label = _launchd_job_fingerprint(service_runtime.LAUNCHD_LABEL)
    if new_candidate_label["state"] != "absent":
        raise EvidenceError("new candidate launchd label must be absent during capture")
    receipt = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": "legacy-launchd-definition",
        "captured_at": datetime.now(UTC).isoformat(),
        "producer_sha256": _sha256_file(Path(__file__)),
        "candidate_commit": candidate_commit,
        "candidate_checkout": candidate,
        "wheel": installed_wheel,
        "cutover_eligible": True,
        "definition": definition,
        "runtime": {
            "identity": runtime["identity"],
            "definition_sha256": runtime["definition_sha256"],
            "wrapper_pid": runtime["wrapper_pid"],
            "listener_pid": runtime["listener_pid"],
            "listener_port": LEGACY_PORT,
            "listener_is_wrapper_child": True,
            "network_requests_sent": observation["network_requests_sent"],
        },
        "new_candidate_label": new_candidate_label,
    }
    receipt_sha256 = _write_terminal(output_path, receipt)
    return {
        "status": "completed",
        "receipt": str(output_path),
        "receipt_sha256": receipt_sha256,
    }


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
    launchd = subparsers.add_parser("launchd-rehearsal")
    launchd.add_argument("--output-dir", required=True)
    launchd.add_argument("--wheel", required=True)
    launchd.add_argument("--candidate-repo", required=True)
    launchd.add_argument("--candidate-commit", required=True)
    launchd.add_argument("--foreground-receipt", required=True)
    launchd.add_argument("--foreground-receipt-sha256", required=True)
    launchd.add_argument("--label", required=True)
    launchd.add_argument("--port", type=int, required=True)
    launchd.add_argument("--timeout-seconds", type=float, default=30.0)
    launchd.add_argument("--allow-missing-legacy-listener", action="store_true")
    legacy = subparsers.add_parser("legacy-launchd-snapshot")
    legacy.add_argument("--output", required=True)
    legacy.add_argument("--wheel", required=True)
    legacy.add_argument("--candidate-repo", required=True)
    legacy.add_argument("--candidate-commit", required=True)
    restore_acceptance.add_evidence_subcommand(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "runtime-rehearsal":
            result = run_runtime_rehearsal(
                output_directory=Path(args.output_dir),
                wheel=Path(args.wheel),
                candidate_repository=Path(args.candidate_repo),
                candidate_commit=args.candidate_commit,
                port=args.port,
                timeout_seconds=args.timeout_seconds,
                require_legacy_listener=not args.allow_missing_legacy_listener,
            )
        elif args.command == "launchd-rehearsal":
            result = run_launchd_rehearsal(
                output_directory=Path(args.output_dir),
                wheel=Path(args.wheel),
                candidate_repository=Path(args.candidate_repo),
                candidate_commit=args.candidate_commit,
                foreground_receipt=Path(args.foreground_receipt),
                foreground_receipt_sha256=args.foreground_receipt_sha256,
                label=args.label,
                port=args.port,
                timeout_seconds=args.timeout_seconds,
                require_legacy_listener=not args.allow_missing_legacy_listener,
            )
        elif args.command == "legacy-launchd-snapshot":
            result = write_legacy_launchd_snapshot(
                output_path=Path(args.output),
                wheel=Path(args.wheel),
                candidate_repository=Path(args.candidate_repo),
                candidate_commit=args.candidate_commit,
            )
        elif args.command == "restore-rehearsal":
            result = restore_acceptance.run_from_evidence_args(args)
        else:  # pragma: no cover - argparse owns it
            raise EvidenceError(f"unsupported evidence command: {args.command}")
    except (
        EvidenceError,
        restore_acceptance.RestoreAcceptanceError,
        restore_acceptance.migration.MigrationError,
        OSError,
        service_runtime.ServiceError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"agentstack-mail-evidence: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
